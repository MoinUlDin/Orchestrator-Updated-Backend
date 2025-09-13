# core/tasks.py
import logging
from django.utils import timezone
import time
from .models import Deployment, Tenant
from typing import Optional, Dict, Any, Tuple
from django.conf import settings
from .dokploy_client import (
    create_project, create_application, DokployError, save_git_provider,
    save_build_type,create_postgres, deploy_application,
    get_latest_postgres_entry_for_project,
    deploy_postgres, create_domain, save_environment
)

logger = logging.getLogger(__name__)


# retry/poll tunables (can be overridden in Django settings)
DB_POLL_MAX_ATTEMPTS = getattr(settings, "DB_POLL_MAX_ATTEMPTS", 6)
DB_POLL_BASE_WAIT = getattr(settings, "DB_POLL_BASE_WAIT", 3)
# other waits you already used in your flow — keep defaults if not provided
SERVICE_WAIT_AFTER_DEPLOY = getattr(settings, "SERVICE_WAIT_AFTER_DEPLOY", 8)
DELAY_STEP = getattr(settings, "DELAY_STEP", 2)


def _append_log(deployment: Deployment, message: str, meta: Optional[Dict] = None, type: str = "info"):
    deployment.meta = deployment.meta or {}
    logs = deployment.meta.get("logs", [])
    logs.append({"ts": timezone.now().isoformat(), "message": message, "type": type, "meta": meta or {}})
    deployment.meta["logs"] = logs
    deployment.save(update_fields=["meta", "updated_at"])

def _persist_last_error(deployment: Deployment, step: str, err: str, resp: Optional[Any] = None):
    deployment.meta = deployment.meta or {}
    deployment.meta["last_error"] = {"step": step, "error": str(err), "resp": resp}
    deployment.save(update_fields=["meta", "updated_at"])

def _ensure_running_status(deployment: Deployment):
    if deployment.status != "running":
        deployment.status = "running"
        if not deployment.started_at:
            deployment.started_at = timezone.now()
        deployment.save(update_fields=["status", "started_at", "updated_at"])


# -------------------------
# step: ensure project exists (idempotent)
# -------------------------
def _step_ensure_project(deployment: Deployment, tenant: Tenant) -> bool:
    """
    If deployment.meta contains 'dokploy_project_id' -> skip (returns True).
    If created now -> persist id and return True.
    Returns True if created or already present, False on error.
    """
    if (deployment.meta or {}).get("dokploy_project_id"):
        _append_log(
            deployment,
            "project.create skipped - dokploy_project_id already present",
            {"project_id": deployment.meta.get("dokploy_project_id")},
        )
        print("\n ===== project.create skipped - dokploy_project_id already present ====== \n")
        return True

    _ensure_running_status(deployment)
    _append_log(deployment, "Starting project.create step")

    project_name = f"{tenant.project.slug}-{tenant.subdomain}"
    print(f"\n ===== Creating Project with Name {project_name} ====== \n")
    try:
        resp = create_project(name=project_name, description=f"Tenant {tenant.name}")
    except DokployError as e:
        _append_log(deployment, "create_project failed", {"error": str(e)}, type="error")
        _persist_last_error(deployment, "project.create", str(e))
        print(f"\n ===== create_project failed {project_name} Error: {e} ====== \n")
        logger.exception("create_project DokployError for deployment=%s: %s", deployment.id, e)
        return False
    except Exception as e:
        _append_log(deployment, "create_project unexpected error", {"error": str(e)}, type="error")
        _persist_last_error(deployment, "project.create", str(e))
        print(f"\n ===== create_project unexpected error {project_name} Error: {e} ====== \n")
        logger.exception("create_project unexpected error for deployment=%s: %s", deployment.id, e)
        return False

    # extract project id defensively
    print(f"\n ===== checking Project ID ====== \n")
    proj_id = None
    if isinstance(resp, dict):
        proj_id = resp.get("projectId") or resp.get("id") or resp.get("_id")
    else:
        proj_id = str(resp).strip() or None

    if not proj_id:
        _append_log(deployment, "create_project returned no project id", {"resp": resp}, type="error")
        _persist_last_error(deployment, "project.create", "no project id in response", resp)
        logger.error("create_project returned no id for deployment=%s resp=%s", deployment.id, resp)
        print(f"\n ===== create_project returned no project id, Response: {resp} ====== \n")
        return False

    deployment.meta = deployment.meta or {}
    deployment.meta["dokploy_project_id"] = proj_id
    deployment.save(update_fields=["meta", "updated_at"])

    _append_log(deployment, "project.create done", {"project_id": proj_id})
    logger.info("project.create successful for deployment=%s project_id=%s", deployment.id, proj_id)
    print(f"\n ===== project.create successful for deployment ====== \n")
    return True


# -------------------------
# step: ensure Services applications exist (idempotent)
# -------------------------
def _step_ensure_service_app(deployment: Deployment, tenant: Tenant, serv_type: str) -> Tuple[bool, bool]:
    """
    Create single service app of type `serv_type` (backend/frontend) in Dokploy if missing.

    Returns (created_any, success_all):
      - created_any True if we created the app in this run.
      - success_all True if the app is present (either existed or created).
      - success_all False indicates failure (caller should stop/resume later).
    """
    # find single service of given type (single-service assumption)
    ts = tenant.services.filter(service_type=serv_type).first()
    if not ts:
        _append_log(deployment, f"No {serv_type} service configured on template; skipping {serv_type} create")
        return False, True

    # already created on the TenantService
    if ts.app_id:
        _append_log(deployment, f"{serv_type} app already exists for service {ts.name}", {"app_id": ts.app_id})
        # ensure meta.apps contains mapping for bookkeeping
        deployment.meta = deployment.meta or {}
        deployment.meta.setdefault("apps", {})[str(ts.id)] = ts.app_id
        deployment.save(update_fields=["meta", "updated_at"])
        return False, True

    # create the app in Dokploy (main_deployment_function must have ensured dokploy_project_id exists)
    project_id = (deployment.meta or {}).get("dokploy_project_id")
    if not project_id:
        _append_log(deployment, f"Cannot create {serv_type} app: missing dokploy_project_id", type="error")
        _persist_last_error(deployment, f"services.{serv_type}.create", "missing dokploy_project_id")
        return False, False

    app_name = f"{tenant.subdomain}-{ts.name}".replace(" ", "-")[:200]
    _append_log(deployment, f"Creating {serv_type} application in Dokploy for service {ts.name}", {"app_name": app_name})

    try:
        resp = create_application(project_id=project_id, name=app_name, description=f"{ts.name} for {tenant.name}")
    except DokployError as e:
        _append_log(deployment, f"create_application failed for {ts.name}", {"error": str(e)}, type="error")
        _persist_last_error(deployment, f"services.{serv_type}.create", str(e))
        return False, False
    except Exception as e:
        _append_log(deployment, f"create_application unexpected error for {ts.name}", {"error": str(e)}, type="error")
        _persist_last_error(deployment, f"services.{serv_type}.create", str(e))
        return False, False

    # extract id
    app_id = None
    if isinstance(resp, dict):
        app_id = resp.get("applicationId") or resp.get("id") or resp.get("_id")
    else:
        app_id = str(resp).strip() or None

    if not app_id:
        _append_log(deployment, f"create_application returned no id for {ts.name}", {"resp": resp}, type="error")
        _persist_last_error(deployment, f"services.{serv_type}.create", "no_app_id_in_response", resp)
        return False, False

    # persist to TenantService and deployment.meta
    ts.app_id = app_id
    ts.created = True
    ts.save(update_fields=["app_id", "created", "updated_at"])

    deployment.meta = deployment.meta or {}
    deployment.meta.setdefault("apps", {})[str(ts.id)] = app_id
    deployment.save(update_fields=["meta", "updated_at"])

    _append_log(deployment, f"Created {serv_type} application for {ts.name}", {"app_id": app_id})
    return True, True


# -------------------------
# step: Setting up git provider (idempotent)
# -------------------------
def _step_set_git_provider(deployment: Deployment, tenant: Tenant, serv_type: str) -> bool:
    """
    Attach git provider for the first service of type `serv_type` (idempotent).
    Returns True on success/skip, False on failure (caller should stop/resume).
    """
    ts = tenant.services.filter(service_type=serv_type).first()
    if not ts or not ts.app_id:
        _append_log(deployment, f"{serv_type} not ready for git attach; skipping")
        return True

    # pick repo from tenant override or template as elsewhere in your code
    repo_url = ts.repo_url or (ts.service_template.repo_url if ts.service_template else None)
    branch = ts.repo_branch or (ts.service_template.repo_branch if ts.service_template else "main")
    if not repo_url:
        _append_log(deployment, f"No repo configured for {serv_type} {ts.name}; skipping git attach")
        return True

    if ts.git_attached:  # field in TenantService
        _append_log(deployment, f"Git already attached for {ts.name}", {"repo": repo_url})
        return True

    _append_log(deployment, f"Attaching Git repo {repo_url} to {serv_type} app {ts.name}", {"branch": branch})
    try:
        resp = save_git_provider(
            application_id=ts.app_id,
            custom_git_url=repo_url,
            branch=branch,
            # optionally: build_path=(ts.service_template.build_config.get("buildPath","/") if ts.service_template else "/")
        )
    except DokployError as e:
        _append_log(deployment, "attach_git_provider failed", {"error": str(e)}, type="error")
        _persist_last_error(deployment, f"services.{serv_type}.git_attach", str(e))
        return False
    except Exception as e:
        _append_log(deployment, "attach_git_provider unexpected error", {"error": str(e)}, type="error")
        _persist_last_error(deployment, f"services.{serv_type}.git_attach", str(e))
        return False

    ts.git_attached = True
    ts.save(update_fields=["git_attached", "updated_at"])
    _append_log(deployment, f"Git attached successfully to {ts.name}", {"resp": resp})
    return True

# -------------------------
# step: Setting up Build Config (idempotent)
# -------------------------
def _step_set_build_config(deployment: Deployment, tenant: Tenant, serv_type: str) -> bool:
    """
    Configure build type for the first service of type `serv_type`.
    Idempotent: if ts.build_configured True -> skip.
    Returns True on success/skip, False on failure (caller should stop/resume).
    """
    ts = tenant.services.filter(service_type=serv_type).first()
    if not ts or not ts.app_id:
        _append_log(deployment, f"{serv_type} not ready for build config; skipping")
        return True

    if ts.build_configured:
        _append_log(deployment, f"Build already configured for {ts.name}", {"app_id": ts.app_id})
        return True

    # get build config from service_template if present
    bcfg = {}
    if ts.service_template and isinstance(ts.service_template.build_config, dict):
        bcfg = ts.service_template.build_config.copy()

    # determine values with sensible defaults
    build_type = bcfg.get("buildType") or "dockerfile"
    dockerfile = bcfg.get("dockerfile", "./DockerFile")
    docker_context_path = bcfg.get("dockerContextPath", "") or ""
    docker_build_stage = bcfg.get("dockerBuildStage", "") or ""
    is_static_spa = bool(bcfg.get("isStaticSpa")) or (ts.service_type == "frontend" and bcfg.get("isStaticSpa"))
    publish_directory = bcfg.get("publishDirectory") or bcfg.get("publish_dir") or None

    _append_log(deployment, f"Saving build type for {serv_type} {ts.name}", {
        "build_type": build_type,
        "dockerfile": dockerfile,
        "publish_directory": publish_directory,
    })

    try:
        save_build_type(
            application_id=ts.app_id,
            build_type=build_type,
            dockerfile=dockerfile,
            docker_context_path=docker_context_path,
            docker_build_stage=docker_build_stage,
            is_static_spa=is_static_spa,
            publish_directory=publish_directory,
        )
    except DokployError as e:
        _append_log(deployment, f"save_build_type failed for {ts.name}", {"error": str(e)}, type="error")
        _persist_last_error(deployment, f"services.{serv_type}.build_config", str(e))
        return False
    except Exception as e:
        _append_log(deployment, f"save_build_type unexpected error for {ts.name}", {"error": str(e)}, type="error")
        _persist_last_error(deployment, f"services.{serv_type}.build_config", str(e))
        return False

    ts.build_configured = True
    ts.save(update_fields=["build_configured", "updated_at"])
    _append_log(deployment, f"Build config saved for {ts.name}", {"build_type": build_type, "app_id": ts.app_id})
    return True


# -------------------------
# step: create DB (project-level, single DB)
# returns (created, success)
# -------------------------
def _step_create_db(deployment: Deployment, tenant: Tenant) -> Tuple[bool, bool]:
    """
    Create postgres resource for the project if project.template requires DB.
    Returns (created_this_run, success_final).
      - created_this_run True if we invoked create_postgres during this call.
      - success_final True if DB is present and deploy_postgres was triggered successfully.
      - success_final False indicates creation attempted but incomplete/failed (caller should stop/resume).
    """
    if not tenant.project.db_required:
        _append_log(deployment, "Project template does not require DB; skipping db.create")
        return False, True

    # If we already have postgres info, skip
    if (deployment.meta or {}).get("postgres_id") or (deployment.meta or {}).get("db_credentials"):
        _append_log(deployment, "DB already created/recorded in deployment.meta; skipping")
        return False, True

    project_id = (deployment.meta or {}).get("dokploy_project_id")
    if not project_id:
        _append_log(deployment, "Cannot create DB: missing dokploy_project_id in deployment.meta", type="error")
        _persist_last_error(deployment, "db.create", "missing dokploy_project_id")
        return False, False

    # prepare db names & creds (persist password to meta so resume reuses it)
    db_name = f"{tenant.project.slug}_{tenant.subdomain}".lower().replace("-", "_")[:48] + "_db"
    db_user = f"{tenant.subdomain[:10]}_user"
    deployment.meta = deployment.meta or {}
    db_pass = deployment.meta.get("db_password")
    created_now = False

    if not db_pass:
        import secrets, string
        alphabet = string.ascii_letters + string.digits
        db_pass = "".join(secrets.choice(alphabet) for _ in range(20))
        deployment.meta["db_password"] = db_pass
        deployment.save(update_fields=["meta", "updated_at"])

    _append_log(deployment, "Requesting postgres resource creation", {"database": db_name})

    # 1) Call create_postgres (your dokploy_client helper)
    try:
        create_postgres(
            project_id=project_id,
            name=f"{tenant.project.slug}-db",
            app_name=f"{tenant.project.slug[:20]}",
            database_name=db_name,
            database_user=db_user,
            database_password=db_pass,
            docker_image="postgres:15",
        )
        created_now = True
    except DokployError as e:
        _append_log(deployment, "create_postgres failed", {"error": str(e)}, type="error")
        _persist_last_error(deployment, "db.create", str(e))
        return False, False
    except Exception as e:
        _append_log(deployment, "create_postgres unexpected error", {"error": str(e)}, type="error")
        _persist_last_error(deployment, "db.create", str(e))
        return False, False

    time.sleep(2)
    # 2) Poll for postgres entry using get_latest_postgres_entry_for_project
    poll_attempt = 0
    pg_entry = None
    while poll_attempt < DB_POLL_MAX_ATTEMPTS:
        try:
            pg_entry = get_latest_postgres_entry_for_project(project_id)
        except DokployError as e:
            _append_log(deployment, f"project.one unreachable while polling for postgres: {e}", type="error")
            _persist_last_error(deployment, "db.create", f"project.one unreachable: {e}")
            return (created_now, False)

        if pg_entry:
            break

        poll_attempt += 1
        wait = DB_POLL_BASE_WAIT * (2 ** (poll_attempt - 1))
        _append_log(deployment, f"db.create poll attempt {poll_attempt} - postgres not visible yet; waiting {wait}s")
        time.sleep(wait)

    if not pg_entry:
        _append_log(deployment, "postgres entry not visible after polls", type="error")
        _persist_last_error(deployment, "db.create", "postgres not visible after polls")
        return (created_now, False)

    # extract id and persist credentials
    pg_id = pg_entry.get("postgresId") or pg_entry.get("id")
    print(f"\n=============\npg_entry Full: {pg_entry}\n=============\n" )
    deployment.meta = deployment.meta or {}
    deployment.meta["postgres_id"] = pg_id
    deployment.meta["db_credentials"] = {
        "DB_NAME": pg_entry.get("databaseName"),
        "DB_USER": pg_entry.get("databaseUser"),
        "DB_PASSWORD": pg_entry.get("databasePassword"),
        "DB_HOST": pg_entry.get("appName") or pg_entry.get("name"),
        "DB_PORT": pg_entry.get("externalPort") or 5432,
    }
    deployment.save(update_fields=["meta", "updated_at"])

    _append_log(deployment, "Postgres entry found, triggering postgres.deploy", {"postgres_id": pg_id})

    # 3) Trigger deploy_postgres (use your helper)
    try:
        deploy_postgres(pg_id)
    except DokployError as e:
        _append_log(deployment, "deploy_postgres failed", {"error": str(e)}, type="error")
        _persist_last_error(deployment, "db.deploy", str(e))
        # DB entry exists but deploy failed to trigger — mark incomplete so caller can retry/resume
        return (created_now, False)
    except Exception as e:
        _append_log(deployment, "deploy_postgres unexpected error", {"error": str(e)}, type="error")
        _persist_last_error(deployment, "db.deploy", str(e))
        return (created_now, False)

    _append_log(deployment, "db.create and db.deploy succeeded", {"postgres": pg_entry})
    return (created_now, True)

# -------------------------
# step: create domain for a service (idempotent)
# -------------------------
def _step_create_domain(deployment: Deployment, tenant: Tenant, serv_type: str) -> bool:
    """
    Ensure domain is created for the given service (frontend/backend).
    Idempotent: skips if TenantService already has a domain_id set.
    """
    ts = tenant.services.filter(service_type=serv_type).first()
    if not ts:
        _append_log(deployment, f"No {serv_type} service configured; skipping domain creation")
        return True

    if not ts.app_id:
        _append_log(deployment, f"Cannot create domain: {serv_type} app_id missing", type="error")
        _persist_last_error(deployment, f"services.{serv_type}.domain", "missing_app_id")
        return False

    if ts.domain_id:
        _append_log(deployment, f"Domain already exists for {serv_type}", {"domain_id": ts.domain_id})
        return True

    # build host name
    project = tenant.project
    if not project or not project.base_domain:
        _append_log(deployment, f"Cannot create domain: missing base_domain for {serv_type}", type="error")
        _persist_last_error(deployment, f"services.{serv_type}.domain", "missing_base_domain")
        return False

    if serv_type == "frontend":
        host = f"{tenant.subdomain}.{project.base_domain}"
    else:  # backend
        host = f"{tenant.subdomain}-backend.{project.base_domain}"

    _append_log(deployment, f"Creating domain for {serv_type}", {"host": host})

    try:
        resp = create_domain(application_id=ts.app_id, host=host)
    except DokployError as e:
        _append_log(deployment, f"create_domain failed for {serv_type}", {"error": str(e)}, type="error")
        _persist_last_error(deployment, f"services.{serv_type}.domain", str(e))
        return False
    except Exception as e:
        _append_log(deployment, f"Unexpected error in create_domain for {serv_type}", {"error": str(e)}, type="error")
        _persist_last_error(deployment, f"services.{serv_type}.domain", str(e))
        return False

    domain_id = None
    if isinstance(resp, dict):
        domain_id = resp.get("domainId") or resp.get("id") or resp.get("_id")

    if not domain_id:
        _append_log(deployment, f"create_domain returned no id for {serv_type}", {"resp": resp}, type="error")
        _persist_last_error(deployment, f"services.{serv_type}.domain", "no_domain_id_in_response", resp)
        return False

    ts.domain = host
    ts.domain_id = domain_id
    ts.save(update_fields=["domain", "domain_id", "updated_at"])

    _append_log(deployment, f"Domain created for {serv_type}", {"domain_id": domain_id, "host": host})
    return True

# -------------------------
# step: Setup Env's for backend (idempotent)
# -------------------------

# -------------------------
# step: set backend environment vars (idempotent)
# -------------------------
def _step_set_backend_env(deployment: Deployment, tenant: Tenant) -> bool:
    """
    Set env vars for backend service: DB credentials + allowed hosts/origins + template envs.
    Idempotent:
      - If backend service not present or no app_id => skip (return True).
      - On successful save => mark ts.env_configured True and return True.
      - On transient/fatal error => log and return False (caller should stop/resume).
    Reads DB credentials from deployment.meta['db_credentials'] created by _step_create_db.
    """
    # find backend service (single backend model)
    ts = tenant.services.filter(service_type="backend").first()
    if not ts:
        _append_log(deployment, "No backend service configured; skipping backend env setup")
        return True
    if ts.env_configured:
        _append_log(deployment, "Env already configured; skipping kipping backend env setup")
        print("\nEnv already configured; skipping backend env setup\n")
        return True
    
    if not ts.app_id:
        _append_log(deployment, f"Backend service {ts.name} has no app_id yet; skipping env setup")
        return True
    

    # db credentials are stored in deployment.meta by _step_create_db
    db_creds = (deployment.meta or {}).get("db_credentials") or {}
    if tenant.project.db_required and not db_creds:
        _append_log(deployment, "DB required but db_credentials missing in deployment.meta; cannot set backend env", type="error")
        _persist_last_error(deployment, "backend.env", "missing_db_credentials")
        return False

    # compose domains
    base_domain = getattr(tenant.project, "base_domain", None)
    if not base_domain:
        _append_log(deployment, "Missing project.base_domain; cannot set backend env", type="error")
        _persist_last_error(deployment, "backend.env", "missing_base_domain")
        return False

    front_host = f"{tenant.subdomain}.{base_domain}"
    backend_host = f"{tenant.subdomain}-backend.{base_domain}"

    # build env lines
    env_lines = []

    # DB vars if present
    if db_creds:
        env_lines.extend([
            f"DB_NAME={db_creds.get('DB_NAME','')}",
            f"DB_USER={db_creds.get('DB_USER','')}",
            f"DB_PASSWORD={db_creds.get('DB_PASSWORD','')}",
            f"DB_HOST={db_creds.get('DB_HOST','')}",
            f"DB_PORT={db_creds.get('DB_PORT', 5432)}",
        ])

    # Allowed hosts & CORS/CSRF
    allowed_hosts = f"{front_host},{backend_host},localhost,127.0.0.1"
    env_lines.append(f"ALLOWED_HOSTS={allowed_hosts}")

    # For CSRF_TRUSTED_ORIGINS and CORS_ALLOWED_ORIGINS use https scheme
    csrf_list = [f"https://{front_host}", f"https://{backend_host}"]
    cors_list = [f"https://{front_host}"]

    # join lists as comma separated (your parse_list_env accepts JSON or separators; commas are fine)
    env_lines.append(f"CSRF_TRUSTED_ORIGINS={','.join(csrf_list)}")
    env_lines.append(f"CORS_ALLOWED_ORIGINS={','.join(cors_list)}")

    # Add provision callback token if template provided one
    prov_token = ts.service_template.internal_provision_token_secret if ts.service_template else None
    if prov_token:
        env_lines.append(f"PROVISION_CALLBACK_TOKEN={prov_token}")

    # Add any env_vars defined on the service_template (preserve name/value)
    for ev in (ts.service_template.env_vars or []):
        if isinstance(ev, dict) and ev.get("name"):
            # allow value to be empty string if not present
            env_lines.append(f"{ev['name']}={ev.get('value','')}")

    env_payload = "\n".join(env_lines)

    _append_log(deployment, f"Setting environment for backend {ts.name}", {"vars_count": len(env_lines)})

    try:
        # save_environment is your dokploy_client helper (application.saveEnvironment)
        save_environment(application_id=ts.app_id, env_str=env_payload)
    except DokployError as e:
        _append_log(deployment, f"save_environment failed for backend {ts.name}", {"error": str(e)}, type="error")
        _persist_last_error(deployment, "backend.env", str(e))
        return False
    except Exception as e:
        _append_log(deployment, f"save_environment unexpected error for backend {ts.name}", {"error": str(e)}, type="error")
        _persist_last_error(deployment, "backend.env", str(e))
        return False

    # mark configured
    ts.env_configured = True
    ts.save(update_fields=["env_configured", "updated_at"])

    _append_log(deployment, f"Backend environment saved for {ts.name}", {"app_id": ts.app_id})
    return True


# ----------------------------------
# Step: Deploy a service
# ----------------------------------
def _step_deploy_service(deployment: Deployment, tenant: Tenant, serv_type: str) -> bool:
    """
    Deploys a tenant service (backend/frontend).
    Returns True if deployment triggered successfully, False otherwise.
    """
    ts = tenant.services.filter(service_type=serv_type).first()
    if not ts:
        print(f"\n No {serv_type} service configured; skipping \n")
        _append_log(deployment, f"No {serv_type} service configured; skipping")
        return True
    
    if not ts.app_id:
        logger.error("_step_deploy_service: service %s has no app_id", serv_type)
        print(f"\n _step_deploy_service: service {serv_type} has no app_id \n")
        return False

    try:
        logger.info("Deploying service=%s (app_id=%s)", serv_type, ts.app_id)
        resp = deploy_application(ts.app_id)
        logger.debug("_step_deploy_service response: %s", resp)
        ts.deploy_triggered = True
        ts.save(update_fields=["deploy_triggered", "updated_at"])
        _append_log(deployment, f"Deployment triggered for {serv_type} service.")
        return True
    except Exception as e:
        logger.exception("_step_deploy_service: failed for %s: %s", serv_type, e)
        return False
    
# -------------------------
# main_deployment_function
# -------------------------
def main_deployment_function(deployment_id: int):
    """
    This is the main orchestrator function invoked by APScheduler.
    It MUST call step functions directly (no further scheduling).
    """
    print("\n ===== Got inside Main ====== \n")
    try:
        deployment = Deployment.objects.select_related("tenant", "triggered_by").get(id=deployment_id)
    except Deployment.DoesNotExist:
        logger.error("main_deployment_function: deployment not found %s", deployment_id)
        return

    tenant: Tenant = deployment.tenant
    logger.info("main_deployment_function starting for deployment=%s tenant=%s", deployment_id, tenant.id)

    # Step 1: ensure project exists
    print("\n ===== Checking Project ====== \n")
    ok = _step_ensure_project(deployment, tenant)
    if not ok:
        return

    # Step 2: ensure backend exists
    print(f"\n ===== Checking Backend with {DELAY_STEP} seconds delay ====== \n")
    time.sleep(DELAY_STEP)
    created_any, success_all = _step_ensure_service_app(deployment, tenant, 'backend')
    if not success_all:
        logger.info("main_deployment_function: backend creation failed or incomplete; exiting for deployment=%s", deployment_id)
        return

    # Step 3: attach git provider (idempotent)
    ok_git = _step_set_git_provider(deployment, tenant, 'backend')
    if not ok_git:
        logger.info("main_deployment_function: git attach failed for backend; exiting for deployment=%s", deployment_id)
        return

    # Step 4: save build config for backend
    ok_build_backend = _step_set_build_config(deployment, tenant, 'backend')
    if not ok_build_backend:
        logger.info("main_deployment_function: build config failed for backend; exiting for deployment=%s", deployment_id)
        return

    # Step 5: create DB if required (returns created_this_run, success)
    created_db, db_ok = _step_create_db(deployment, tenant)
    if not db_ok:
        logger.info("main_deployment_function: db creation incomplete/failed; exiting for deployment=%s", deployment_id)
        return
    

    # Step 6: create frontend service
    print(f'\n ===== Waiting for {DELAY_STEP} seconds before creating fronted service')
    time.sleep(DELAY_STEP)
    created_frontend, frontend_ok = _step_ensure_service_app(deployment, tenant, 'frontend')
    if not frontend_ok:
        logger.info("main_deployment_function: frontend creation incomplete/failed; exiting for deployment=%s", deployment_id)
        return
    
    # Step 7: set git provider for frontend
    ok_frontend_git = _step_set_git_provider(deployment, tenant, 'frontend')
    if not ok_frontend_git:
        logger.info("main_deployment_function: git attach failed for frontend; exiting for deployment=%s", deployment_id)
        return

    # Step 8: set build config for frontend
    ok_build_frontend = _step_set_build_config(deployment, tenant, 'frontend')
    if not ok_build_frontend:
        logger.info("main_deployment_function: build config failed for frontend; exiting for deployment=%s", deployment_id)
        return
    
    time.sleep(DELAY_STEP)
     # Step 9: create domain for backend
    ok_backend_domain = _step_create_domain(deployment, tenant, 'backend')
    if not ok_backend_domain:
        logger.info("main_deployment_function: backend domain creation failed; exiting for deployment=%s", deployment_id)
        return

    # Step 10: create domain for frontend
    ok_frontend_domain = _step_create_domain(deployment, tenant, 'frontend')
    if not ok_frontend_domain:
        logger.info("main_deployment_function: frontend domain creation failed; exiting for deployment=%s", deployment_id)
        return
    
    # Step 11: set backend environment variables
    ok_backend_env = _step_set_backend_env(deployment, tenant)
    if not ok_backend_env:
        logger.info("main_deployment_function: backend env setup failed; exiting for deployment=%s", deployment_id)
        return
    
    deployment_delay = 180
    # Step 12: deploy backend service
    print('\n Going to trigger Backend service if exists\n')
    ok_backend_deploy = _step_deploy_service(deployment, tenant, 'backend')
    if not ok_backend_deploy:
        logger.info("main_deployment_function: backend deployment failed; exiting for deployment=%s", deployment_id)
        return
    
    # wait 3 minutes before frontend deploy
    logger.info(f"Sleeping {deployment_delay/60} minutes before deploying frontend...")
    _append_log(deployment, f"Sleeping for  {deployment_delay/60} minutes before deploying frontend...")
    print(f"Sleeping {deployment_delay/60} minutes before deploying frontend...")
    time.sleep(deployment_delay)

    # Step 13: deploy frontend service
    ok_frontend_deploy = _step_deploy_service(deployment, tenant, 'frontend')
    if not ok_frontend_deploy:
        logger.info("main_deployment_function: frontend deployment failed; exiting for deployment=%s", deployment_id)
        return

    frontend_delay = 120
    logger.info(f"Sleeping {frontend_delay/60} minutes after frontend deployment triggered.")
    print(f"Sleeping {frontend_delay/60} minutes after frontend deployment triggered.")
    _append_log(deployment, f"Sleeping for  {frontend_delay/60} minutes so frontend deployment finishes")

    time.sleep(frontend_delay)
    
    # IMPORTANT: do not set deployment.status to 'succeeded' here.
    logger.info("main_deployment_function finished checks for deployment=%s", deployment_id)
    print("\n +++++ All given tasks completed (project/backend/git) +++++ \n")
    return


