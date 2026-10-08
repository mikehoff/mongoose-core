"""WordPress plugin build-and-deploy workflow for Mongoose.

The flow: local plugin source → build zip → upload to WP as beta slug → activate/test → promote.
Uses the WordPress REST API (WP 5.5+, needs manage_options capability via Application Password).

Plugin management endpoints:
  GET    /wp-json/wp/v2/plugins              list installed plugins
  POST   /wp-json/wp/v2/plugins              install from zip (multipart/form-data pluginzip=<bytes>)
  PUT    /wp-json/wp/v2/plugins/{slug}       activate / deactivate / update
  DELETE /wp-json/wp/v2/plugins/{slug}       delete plugin

The local source lives at MONGOOSE_PLUGIN_DIR (default: ~/mongoose-plugin).
Build produces a versioned zip under data/builds/plugin/.
"""
from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path
from typing import Any

import httpx

from .. import profile as _profile


# ── helpers ─────────────────────────────────────────────────────────────────

def _creds() -> tuple[str, str, str] | None:
    url = os.getenv("WP_URL", "").strip().rstrip("/")
    user = os.getenv("WP_USERNAME", "").strip()
    pwd = os.getenv("WP_APP_PASSWORD", "").strip()
    return (url, user, pwd) if (url and user and pwd) else None


def _plugin_dir() -> Path:
    d = os.getenv("MONGOOSE_PLUGIN_DIR", "").strip()
    return Path(d).expanduser() if d else Path.home() / "mongoose-plugin"


def _slugify(name: str) -> str:
    return name.strip().lower().replace(" ", "-").replace("_", "-")


def _plugin_slug(src: Path, status: dict[str, Any] | None = None) -> str:
    """The WordPress plugin FOLDER name to package the zip under.

    WordPress keys plugin identity on the folder name, so this has to come from the plugin's own
    identity - the main file carrying the "Plugin Name:" header, which by WordPress convention is
    <slug>/<slug>.php - and never from whatever the local source directory happens to be called.
    Those two disagree here (~/mongoose-plugin contains <plugin_slug>.php), and taking the
    directory name shipped a zip rooted at mongoose-plugin/, which WordPress installs as a SECOND
    plugin beside <plugin_slug> instead of updating it. Falls back to the directory name only
    when no main file can be found.
    """
    if status is None:
        status = plugin_source_status()
    main = status.get("main_file") if isinstance(status, dict) else None
    if main:
        return _slugify(Path(str(main)).stem)
    return _slugify(src.name)


def _build_dir() -> Path:
    data_dir = (os.getenv("MONGOOSE_DATA_DIR") or "").strip() or os.path.expanduser("~/.mongoose/data")
    bd = Path(data_dir) / "builds" / "plugin"
    bd.mkdir(parents=True, exist_ok=True)
    return bd


# ── local build ─────────────────────────────────────────────────────────────

def plugin_source_status() -> dict[str, Any]:
    """Return info about the local plugin source directory."""
    src = _plugin_dir()
    if not src.exists():
        return {"ok": False, "reason": "no_source",
                "path": str(src),
                "hint": f"Create your plugin source at {src} or set MONGOOSE_PLUGIN_DIR in .env"}
    files = sorted(str(p.relative_to(src)) for p in src.rglob("*") if p.is_file()
                   and not any(part.startswith(".") for part in p.relative_to(src).parts))
    # detect the main plugin file (*.php with Plugin Name: header)
    main = None
    version = None
    for php in src.glob("*.php"):
        txt = php.read_text(errors="replace")
        if "Plugin Name:" in txt:
            main = str(php.name)
            for line in txt.splitlines():
                if line.strip().startswith("Version:") or line.strip().lstrip("* ").startswith("Version:"):
                    version = line.split(":", 1)[1].strip()
            break
    return {"ok": True, "path": str(src), "files": files[:50],
            "main_file": main, "version": version,
            "file_count": len(files)}


def build_plugin_zip(version_suffix: str = "beta") -> dict[str, Any]:
    """Zip the plugin source into a deployable archive.
    Produces: data/builds/plugin/<plugin-slug>-<version_suffix>.zip
    Returns {ok, zip_path, slug, size_bytes}."""
    src = _plugin_dir()
    if not src.exists():
        return {"ok": False, "reason": "no_source", "path": str(src),
                "hint": f"Create your plugin source at {src} or set MONGOOSE_PLUGIN_DIR in .env"}
    status = plugin_source_status()
    if not status["ok"]:
        return status
    slug = _plugin_slug(src, status)
    zip_name = f"{slug}-{version_suffix}.zip"
    zip_path = _build_dir() / zip_name
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in src.rglob("*"):
                if file.is_file() and not any(p.startswith(".") for p in file.relative_to(src).parts):
                    zf.write(file, arcname=f"{slug}/{file.relative_to(src)}")
        size = zip_path.stat().st_size
        return {"ok": True, "zip_path": str(zip_path), "slug": slug,
                "version_suffix": version_suffix, "size_bytes": size,
                "file_name": zip_name, "main_file": status.get("main_file"),
                "source_version": status.get("version"), "source_dir": str(src)}
    except Exception as exc:
        return {"ok": False, "reason": f"build_failed: {exc}"}


def list_builds() -> list[dict[str, Any]]:
    """List previously built plugin zips."""
    bd = _build_dir()
    builds = []
    for z in sorted(bd.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        builds.append({"name": z.name, "path": str(z),
                       "size_bytes": z.stat().st_size,
                       "modified": z.stat().st_mtime})
    return builds[:20]


# ── WordPress plugin management ──────────────────────────────────────────────

def wp_list_plugins() -> dict[str, Any]:
    """List all installed plugins on the WP site."""
    creds = _creds()
    if not creds:
        return {"ok": False, "reason": "not_configured", "plugins": []}
    url, user, pwd = creds
    try:
        r = httpx.get(f"{url}/wp-json/wp/v2/plugins",
                      auth=(user, pwd), params={"per_page": 100}, timeout=10)
        if r.status_code == 200:
            plugins = [{"name": p.get("name"), "plugin": p.get("plugin"),
                        "version": p.get("version"), "status": p.get("status"),
                        "description": (p.get("description") or {}).get("raw", "")[:80]}
                       for p in r.json()]
            return {"ok": True, "plugins": plugins}
        return {"ok": False, "reason": f"HTTP {r.status_code}", "detail": r.text[:200]}
    except Exception as exc:
        return {"ok": False, "reason": f"unreachable: {exc}", "plugins": []}


def wp_upload_plugin(zip_path: str | Path, *, overwrite: bool = True) -> dict[str, Any]:
    """Upload a plugin zip to WordPress via the REST API (installs but does not activate).
    Requires manage_plugins capability (Application Password user must be Admin).

    If the plugin is already installed and overwrite=True (default), deactivates the old version,
    deletes it, then re-uploads: so re-deploying an updated build just works without manual steps.
    """
    from .egress_guard import egress_allowed
    if not egress_allowed("wp_plugin_mgmt")[0]:
        return {"ok": False, "reason": "egress_blocked_non_live"}
    creds = _creds()
    if not creds:
        return {"ok": False, "reason": "not_configured"}
    url, user, pwd = creds
    zp = Path(zip_path)
    if not zp.exists():
        return {"ok": False, "reason": f"zip_not_found: {zp}"}

    def _do_upload() -> dict[str, Any]:
        # WP REST API plugin install requires multipart/form-data with field 'pluginzip'
        with open(zp, "rb") as f:
            zip_bytes = f.read()
        files = {"pluginzip": (zp.name, zip_bytes, "application/zip")}
        r = httpx.post(f"{url}/wp-json/wp/v2/plugins", auth=(user, pwd),
                       files=files, timeout=60)
        if r.status_code in (200, 201):
            d = r.json()
            return {"ok": True, "plugin": d.get("plugin"), "name": d.get("name"),
                    "version": d.get("version"), "status": d.get("status")}
        if r.status_code == 400:
            try:
                detail = r.json().get("message", r.text[:300])
            except Exception:
                detail = r.text[:300]
            if "already" in detail.lower() or "exists" in detail.lower():
                return {"ok": False, "reason": "already_installed", "detail": detail}
            # Stock WordPress core /wp/v2/plugins installs ONLY from the wordpress.org directory (it
            # wants a `slug`); it does not accept a custom plugin zip over REST. So the FIRST install of
            # the site's RTM plugin has to be done by hand in WP Admin. Say so plainly with steps.
            low = (detail or "").lower()
            if "slug" in low or "rest_missing_callback_param" in low or "rest_no_route" in low:
                return {"ok": False, "reason": "wp_rest_no_zip_upload", "detail": detail,
                        "manual": True,
                        "hint": "WordPress won't install a custom plugin zip over its REST API (that "
                                "endpoint only installs from the WordPress.org directory). Install it "
                                "once by hand: WP Admin -> Plugins -> Add New -> Upload Plugin -> choose "
                                "the zip -> Install Now -> Activate. After that first manual install, "
                                "Mongoose can verify and sync it."}
        return {"ok": False, "reason": f"HTTP {r.status_code}", "detail": r.text[:300]}

    try:
        result = _do_upload()
        if result.get("reason") == "already_installed" and overwrite:
            # Find the existing plugin slug from the installed list and replace it
            existing = wp_list_plugins()
            if not existing.get("ok"):
                return {"ok": False, "reason": "already_installed_and_list_failed",
                        "hint": "Delete the old plugin from WP Admin then retry."}
            # Match by zip filename stem → plugin slug
            stem = zp.stem.lower().replace("-", "_")
            old_slug = None
            for p in existing.get("plugins", []):
                slug_id = (p.get("plugin") or "").lower().replace("-", "_").replace("/", "_")
                if stem in slug_id or p.get("name", "").lower().replace(" ", "_") in stem:
                    old_slug = p.get("plugin")
                    break
            if not old_slug:
                return {"ok": False, "reason": "already_installed_slug_not_found",
                        "hint": "Plugin already installed but could not match slug: delete it manually from WP Admin."}
            # Deactivate then delete the old version
            deact = wp_set_plugin_status(old_slug, "inactive")
            if not deact.get("ok") and "inactive" not in str(deact.get("detail", "")):
                # May already be inactive: continue anyway
                pass
            del_r = wp_delete_plugin(old_slug)
            if not del_r.get("ok"):
                return {"ok": False, "reason": "overwrite_delete_failed",
                        "detail": del_r.get("reason"), "old_slug": old_slug}
            # Re-upload now that slot is clear
            result = _do_upload()
            if result.get("ok"):
                result["replaced"] = old_slug
        return result
    except Exception as exc:
        return {"ok": False, "reason": f"upload_failed: {exc}"}


def wp_set_plugin_status(plugin_slug: str, status: str) -> dict[str, Any]:
    """Activate or deactivate a plugin. status: 'active' | 'inactive'."""
    if status not in ("active", "inactive"):
        return {"ok": False, "reason": "status must be 'active' or 'inactive'"}
    from .egress_guard import egress_allowed
    if not egress_allowed("wp_plugin_mgmt")[0]:
        return {"ok": False, "reason": "egress_blocked_non_live"}
    creds = _creds()
    if not creds:
        return {"ok": False, "reason": "not_configured"}
    url, user, pwd = creds
    # WP plugin slug format: folder/main-file.php  (URL-encode the slash)
    try:
        r = httpx.put(
            f"{url}/wp-json/wp/v2/plugins/{plugin_slug.replace('/', '%2F')}",
            auth=(user, pwd), json={"status": status}, timeout=15,
        )
        if r.status_code == 200:
            d = r.json()
            return {"ok": True, "plugin": d.get("plugin"), "status": d.get("status")}
        return {"ok": False, "reason": f"HTTP {r.status_code}", "detail": r.text[:300]}
    except Exception as exc:
        return {"ok": False, "reason": f"request_failed: {exc}"}


def wp_delete_plugin(plugin_slug: str) -> dict[str, Any]:
    """Delete (uninstall) a plugin. Plugin must be inactive first."""
    from .egress_guard import egress_allowed
    if not egress_allowed("wp_plugin_mgmt")[0]:
        return {"ok": False, "reason": "egress_blocked_non_live"}
    creds = _creds()
    if not creds:
        return {"ok": False, "reason": "not_configured"}
    url, user, pwd = creds
    try:
        r = httpx.delete(
            f"{url}/wp-json/wp/v2/plugins/{plugin_slug.replace('/', '%2F')}",
            auth=(user, pwd), timeout=15,
        )
        if r.status_code in (200, 204):
            return {"ok": True, "deleted": plugin_slug}
        return {"ok": False, "reason": f"HTTP {r.status_code}", "detail": r.text[:300]}
    except Exception as exc:
        return {"ok": False, "reason": f"request_failed: {exc}"}


# ── full deploy workflow ─────────────────────────────────────────────────────

def deploy_plugin_beta(version_suffix: str = "beta",
                       zip_path: str | Path | None = None) -> dict[str, Any]:
    """Full beta deploy: build zip (or use existing) → upload to WP → return plugin info.
    Does NOT activate: activation is a deliberate separate step.
    Returns {ok, plugin_slug, status, next_step}."""
    # Build if no zip provided
    if zip_path is None:
        build = build_plugin_zip(version_suffix)
        if not build["ok"]:
            return build
        zip_path = build["zip_path"]
    upload = wp_upload_plugin(zip_path)
    if not upload["ok"]:
        # Carry the built zip path so the caller can offer it for manual upload (the WP-REST-no-zip case).
        return {**upload, "zip_path": str(zip_path)}
    # (d) Record what was actually pushed into the shared sync record, so the RTM screen reflects the
    # deploy instead of being blind to it. Atomic on success; a record-write failure must not fail the
    # deploy that already happened.
    try:
        from .rtm_deploy_state import record_deployed
        import hashlib as _hl
        from pathlib import Path as _P
        _h = None
        try:
            _h = _hl.sha256(_P(zip_path).read_bytes()).hexdigest()[:16]
        except Exception:
            _h = None
        record_deployed(version=upload.get("version"), content_hash=_h)
    except Exception:
        pass
    return {
        "ok": True,
        "plugin": upload.get("plugin"),
        "name": upload.get("name"),
        "version": upload.get("version"),
        "status": upload.get("status"),
        "next_step": f"Activate with wp_set_plugin_status('{upload.get('plugin')}', 'active')"
    }


def promote_plugin(beta_slug: str, production_slug: str) -> dict[str, Any]:
    """Activate the beta, deactivate the old production version (atomic swap).
    Both slugs in format 'folder/main-file.php'."""
    act = wp_set_plugin_status(beta_slug, "active")
    if not act["ok"]:
        return {"ok": False, "step": "activate_beta", "detail": act}
    deact = wp_set_plugin_status(production_slug, "inactive")
    if not deact["ok"]:
        # Beta is active but we couldn't deactivate old: log and return partial success
        return {"ok": True, "partial": True, "beta_active": True,
                "production_deactivate_failed": deact.get("reason"),
                "detail": "Beta is active. Failed to deactivate old production plugin: check manually."}
    return {"ok": True, "beta_active": beta_slug, "production_inactive": production_slug,
            "note": "Swap complete. Delete old production plugin when confirmed working."}


# ── Plugin scaffold ──────────────────────────────────────────────────────────

_EXAMPLE_COURT_PLUGIN_PHP = '''\
<?php
/**
 * Plugin Name: {rtm_name} RTM
 * Plugin URI:  https://{site_domain}
 * Description: Right-to-Manage workflow tool for {rtm_name}: service charge summaries,
 *              leaseholder communications, and meeting management. Powered by Mongoose.
 * Version:     {version}
 * Author:      {rtm_name} RTM Company Limited
 * License:     Private
 * Text Domain: {plugin_slug}
 */

if ( ! defined( 'ABSPATH' ) ) exit;

define( 'PC_RTM_VERSION', '{version}' );
define( 'PC_RTM_PLUGIN_DIR', plugin_dir_path( __FILE__ ) );
define( 'PC_RTM_PLUGIN_URL', plugin_dir_url( __FILE__ ) );

// ── Admin menu ────────────────────────────────────────────────────────────────
add_action( 'admin_menu', 'pc_rtm_admin_menu' );
function pc_rtm_admin_menu() {{
    add_menu_page(
        '{rtm_name} RTM',
        'RTM',
        'manage_options',
        '{plugin_slug}',
        'pc_rtm_dashboard_page',
        'dashicons-building',
        30
    );
    add_submenu_page( '{plugin_slug}', 'Service Charges', 'Service Charges',
        'manage_options', 'pc-rtm-charges', 'pc_rtm_charges_page' );
    add_submenu_page( '{plugin_slug}', 'Communications', 'Communications',
        'manage_options', 'pc-rtm-comms', 'pc_rtm_comms_page' );
    add_submenu_page( '{plugin_slug}', 'Seller Packs', 'Seller Packs',
        'manage_options', 'pc-rtm-seller-pack', 'pc_rtm_seller_pack_page' );
    add_submenu_page( '{plugin_slug}', 'Settings', 'Settings',
        'manage_options', 'pc-rtm-settings', 'pc_rtm_settings_page' );
}}

// ── Dashboard page ────────────────────────────────────────────────────────────
function pc_rtm_dashboard_page() {{
    $mongoose_url = get_option( 'pc_rtm_mongoose_url', '' );
    ?>
    <div class="wrap">
    <h1>{rtm_name} RTM</h1>
    <div style="display:flex;gap:20px;flex-wrap:wrap;margin-top:16px">
      <div class="pc-card">
        <h2>Service charges</h2>
        <p>Manage demands, reconcile payments, and issue summaries.</p>
        <a href="?page=pc-rtm-charges" class="button button-primary">Open</a>
      </div>
      <div class="pc-card">
        <h2>Communications</h2>
        <p>Draft and send leaseholder notices and meeting minutes.</p>
        <a href="?page=pc-rtm-comms" class="button button-primary">Open</a>
      </div>
      <?php if ( $mongoose_url ): ?>
      <div class="pc-card">
        <h2>Mongoose assistant</h2>
        <p>Ask questions about the building, voicemails, or RTM decisions.</p>
        <a href="<?php echo esc_url( $mongoose_url ); ?>" class="button" target="_blank">Open Mongoose</a>
      </div>
      <?php endif; ?>
    </div>
    </div>
    <?php
}}

function pc_rtm_charges_page() {{
    // Renders the live demand register from the snapshot Mongoose pushes to /sync/snapshot.
    // Mongoose is the single source of truth; this page only displays what was last pushed.
    $snap_raw = (string) get_option( 'pc_rtm_snapshot', '' );
    $snap_at  = (string) get_option( 'pc_rtm_snapshot_at', '' );
    $snap     = $snap_raw ? json_decode( $snap_raw, true ) : null;
    echo '<div class="wrap"><h1>Service Charges</h1>';
    if ( ! is_array( $snap ) || empty( $snap['flats'] ) ) {{
        echo '<div class="notice notice-warning"><p>No snapshot yet. In Mongoose chat say '
           . '<em>&ldquo;push the portal snapshot&rdquo;</em> (or run <code>mongoose rtm portal publish</code>) '
           . 'and reload this page.</p></div></div>';
        return;
    }}
    if ( isset( $_POST['pc_rtm_request_snapshot'] ) && check_admin_referer( 'pc_rtm_request_snapshot' ) ) {{
        $sent = pc_rtm_send_to_mongoose( 'snapshot_requested', array( 'requested_from' => 'service_charges_admin' ) );
        echo $sent
            ? '<div class="notice notice-success"><p>Asked Mongoose for a fresh snapshot. Reload this page in a moment.</p></div>'
            : '<div class="notice notice-warning"><p>Could not reach Mongoose: set the webhook URL and secret on the Settings page first.</p></div>';
    }}
    $flats   = $snap['flats'];
    $demands = isset( $snap['demands'] ) && is_array( $snap['demands'] ) ? $snap['demands'] : array();
    echo '<p class="description">Pushed by Mongoose ' . esc_html( $snap_at ? $snap_at . ' UTC' : 'recently' )
       . ' &middot; ' . count( $flats ) . ' flats &middot; ' . count( $demands ) . ' demands. '
       . esc_html( $snap['caveat_7day'] ?? '' ) . '</p>';
    echo '<form method="post" style="margin:8px 0">';
    wp_nonce_field( 'pc_rtm_request_snapshot' );
    echo '<button type="submit" name="pc_rtm_request_snapshot" class="button">Ask Mongoose for a fresh snapshot</button></form>';

    echo '<h2>Flats</h2><table class="widefat striped"><thead><tr>'
       . '<th>Flat</th><th>Block</th><th>Balance</th><th>Position</th><th>Payment reference</th>'
       . '</tr></thead><tbody>';
    foreach ( $flats as $f ) {{
        $pos = (string) ( $f['position'] ?? '' );
        $pos_html = $pos === 'clear'
            ? '<span style="color:#1a7f37">clear</span>'
            : ( $pos ? '<span style="color:#b45309">' . esc_html( $pos ) . '</span>' : '&mdash;' );
        echo '<tr><td>' . esc_html( (string) ( $f['flat_id'] ?? '' ) ) . '</td>'
           . '<td>' . esc_html( (string) ( $f['block'] ?? '' ) ) . '</td>'
           . '<td>' . esc_html( (string) ( $f['balance'] ?? '&mdash;' ) ) . '</td>'
           . '<td>' . $pos_html . '</td>'
           . '<td><code>' . esc_html( (string) ( $f['payment_reference_display'] ?? '' ) ) . '</code></td></tr>';
    }}
    echo '</tbody></table>';

    if ( $demands ) {{
        echo '<h2 style="margin-top:24px">Demand register</h2><table class="widefat striped"><thead><tr>'
           . '<th>Document</th><th>Flat</th><th>Period</th><th>Amount</th><th>Due</th><th>Status</th>'
           . '</tr></thead><tbody>';
        foreach ( $demands as $d ) {{
            $st = (string) ( $d['status'] ?? '' );
            $st_html = in_array( $st, array( 'Overdue', 'In recovery' ), true )
                ? '<strong style="color:#b91c1c">' . esc_html( $st ) . '</strong>' : esc_html( $st );
            echo '<tr><td><code>' . esc_html( (string) ( $d['document_number'] ?? '' ) ) . '</code></td>'
               . '<td>' . esc_html( (string) ( $d['flat_id'] ?? '' ) ) . '</td>'
               . '<td>' . esc_html( (string) ( $d['period'] ?? '' ) ) . '</td>'
               . '<td>' . esc_html( (string) ( $d['amount'] ?? '' ) ) . '</td>'
               . '<td>' . esc_html( (string) ( $d['due_date'] ?? '' ) ) . '</td>'
               . '<td>' . $st_html . '</td></tr>';
        }}
        echo '</tbody></table>';
    }}
    if ( ! empty( $snap['bank_details'] ) && is_array( $snap['bank_details'] ) ) {{
        $b = $snap['bank_details'];
        echo '<h2 style="margin-top:24px">Payment details shown to leaseholders</h2><p>'
           . 'Sort code <code>' . esc_html( (string) ( $b['sort_code'] ?? '' ) ) . '</code> &middot; '
           . 'Account <code>' . esc_html( (string) ( $b['account_number'] ?? '' ) ) . '</code></p>'
           . '<p class="description">' . esc_html( (string) ( $b['payment_instruction'] ?? '' ) ) . '</p>';
    }}
    echo '</div>';
}}
function pc_rtm_comms_page() {{
    // Draft and publish leaseholder notices as ordinary WP posts under the rtm-notice category.
    // Publishing triggers pc_rtm_on_publish, so Mongoose is notified of every notice that goes out.
    $notice_out = '';
    if ( isset( $_POST['pc_rtm_notice_submit'] ) && check_admin_referer( 'pc_rtm_notice' ) ) {{
        $title = sanitize_text_field( wp_unslash( $_POST['notice_title'] ?? '' ) );
        $body  = wp_kses_post( wp_unslash( $_POST['notice_body'] ?? '' ) );
        $how   = ( $_POST['notice_action'] ?? '' ) === 'publish' ? 'publish' : 'draft';
        if ( $title && $body ) {{
            $cat = get_category_by_slug( 'rtm-notice' );
            if ( ! $cat ) {{
                $cid = wp_insert_term( 'RTM notice', 'category', array( 'slug' => 'rtm-notice' ) );
                $cat_id = is_wp_error( $cid ) ? 0 : (int) $cid['term_id'];
            }} else {{
                $cat_id = (int) $cat->term_id;
            }}
            $pid = wp_insert_post( array(
                'post_title'   => $title,
                'post_content' => $body,
                'post_status'  => $how,
                'post_category'=> $cat_id ? array( $cat_id ) : array(),
            ), true );
            if ( is_wp_error( $pid ) ) {{
                $notice_out = '<div class="notice notice-error"><p>Could not save: '
                            . esc_html( $pid->get_error_message() ) . '</p></div>';
            }} else {{
                $notice_out = '<div class="notice notice-success"><p>'
                            . ( $how === 'publish' ? 'Published' : 'Saved as draft' )
                            . ': <a href="' . esc_url( get_edit_post_link( $pid ) ) . '">'
                            . esc_html( $title ) . '</a>'
                            . ( $how === 'publish' ? ' (Mongoose has been notified).' : '.' ) . '</p></div>';
            }}
        }} else {{
            $notice_out = '<div class="notice notice-warning"><p>Both a title and body are needed.</p></div>';
        }}
    }}
    echo '<div class="wrap"><h1>Leaseholder Communications</h1>' . $notice_out;
    echo '<h2>New notice</h2><form method="post">';
    wp_nonce_field( 'pc_rtm_notice' );
    echo '<table class="form-table">'
       . '<tr><th><label for="notice_title">Title</label></th>'
       . '<td><input id="notice_title" name="notice_title" class="regular-text" required '
       . 'placeholder="e.g. AGM notice, works update, service charge statement" /></td></tr>'
       . '<tr><th><label for="notice_body">Body</label></th>'
       . '<td><textarea id="notice_body" name="notice_body" rows="8" class="large-text" required></textarea></td></tr>'
       . '</table>'
       . '<p><button class="button" name="notice_action" value="draft">Save draft</button> '
       . '<button class="button button-primary" name="notice_action" value="publish">Publish now</button></p>'
       . '</form>';
    $recent = get_posts( array( 'category_name' => 'rtm-notice', 'numberposts' => 10,
                                'post_status' => array( 'publish', 'draft' ) ) );
    if ( $recent ) {{
        echo '<h2 style="margin-top:24px">Recent notices</h2><table class="widefat striped"><thead><tr>'
           . '<th>Title</th><th>Status</th><th>Date</th><th></th></tr></thead><tbody>';
        foreach ( $recent as $p ) {{
            echo '<tr><td>' . esc_html( $p->post_title ) . '</td>'
               . '<td>' . esc_html( $p->post_status ) . '</td>'
               . '<td>' . esc_html( get_the_date( '', $p ) ) . '</td>'
               . '<td><a href="' . esc_url( get_edit_post_link( $p->ID ) ) . '">edit</a>'
               . ( $p->post_status === 'publish'
                   ? ' &middot; <a href="' . esc_url( get_permalink( $p->ID ) ) . '" target="_blank">view</a>' : '' )
               . '</td></tr>';
        }}
        echo '</tbody></table>';
    }}
    echo '</div>';
}}

// ── Settings page ─────────────────────────────────────────────────────────────
function pc_rtm_settings_page() {{
    if ( isset( $_POST['pc_rtm_save'] ) && check_admin_referer( 'pc_rtm_settings' ) ) {{
        update_option( 'pc_rtm_mongoose_url', sanitize_text_field( $_POST['mongoose_url'] ?? '' ) );
        update_option( 'pc_rtm_webhook_url', sanitize_text_field( $_POST['webhook_url'] ?? '' ) );
        update_option( 'pc_rtm_webhook_secret', sanitize_text_field( $_POST['webhook_secret'] ?? '' ) );
        echo '<div class="notice notice-success"><p>Settings saved.</p></div>';
    }}
    $mongoose_url   = get_option( 'pc_rtm_mongoose_url', '' );
    $webhook_url    = get_option( 'pc_rtm_webhook_url', '' );
    $webhook_secret = get_option( 'pc_rtm_webhook_secret', '' );
    ?>
    <div class="wrap"><h1>RTM Settings</h1>
    <form method="post">
    <?php wp_nonce_field( 'pc_rtm_settings' ); ?>
    <h2 class="title">Mongoose connection</h2>
    <table class="form-table">
      <tr><th>Mongoose LAN URL</th>
        <td><input name="mongoose_url" value="<?php echo esc_attr( $mongoose_url ); ?>"
             class="regular-text" placeholder="http://192.168.x.x:8765" />
        <p class="description">Local address of Mongoose: for the Open Mongoose button.</p></td></tr>
      <tr><th>Webhook ingest URL</th>
        <td><input name="webhook_url" value="<?php echo esc_attr( $webhook_url ); ?>"
             class="regular-text" placeholder="https://yourname.duckdns.org:8765/webhook/ingest" />
        <p class="description">From Mongoose cockpit Settings → Webhook → copy Ingest URL.</p></td></tr>
      <tr><th>Webhook secret</th>
        <td><input name="webhook_secret" type="password" value="<?php echo esc_attr( $webhook_secret ); ?>"
             class="regular-text" placeholder="(paste from Mongoose)" />
        <p class="description">From Mongoose cockpit Settings → Webhook → Generate secret.</p></td></tr>
    </table>
    <p><input type="submit" name="pc_rtm_save" class="button-primary" value="Save settings" /></p>
    </form></div>
    <?php
}}

// ── Webhook: send events to Mongoose over DDNS ───────────────────────────────
function pc_rtm_send_to_mongoose( string $event, array $payload = [] ): bool {{
    $url    = get_option( 'pc_rtm_webhook_url', '' );
    $secret = get_option( 'pc_rtm_webhook_secret', '' );
    if ( ! $url || ! $secret ) return false;
    $body = wp_json_encode( array_merge( $payload, [
        'event'  => $event,
        'source' => '{site_domain}',
        'site'   => get_bloginfo( 'url' ),
    ] ) );
    $sig = 'sha256=' . hash_hmac( 'sha256', $body, $secret );
    wp_remote_post( $url, [
        'body'     => $body,
        'headers'  => [
            'Content-Type'         => 'application/json',
            'X-Mongoose-Signature' => $sig,
            'X-Site-Domain'        => '{site_domain}',
        ],
        'timeout'  => 5,
        'blocking' => false,
    ] );
    return true;
}}

// Blocking signed call to Mongoose that RETURNS the decoded JSON (unlike the fire-and-forget
// pc_rtm_send_to_mongoose). Used by the Seller Packs page to fetch fresh, printable document links.
function pc_rtm_call_mongoose( string $event, array $payload = [] ) {{
    $url    = get_option( 'pc_rtm_webhook_url', '' );
    $secret = get_option( 'pc_rtm_webhook_secret', '' );
    if ( ! $url || ! $secret ) return null;
    $body = wp_json_encode( array_merge( $payload, [
        'event'  => $event,
        'source' => '{site_domain}',
        'site'   => get_bloginfo( 'url' ),
    ] ) );
    $sig = 'sha256=' . hash_hmac( 'sha256', $body, $secret );
    $resp = wp_remote_post( $url, [
        'body'     => $body,
        'headers'  => [
            'Content-Type'         => 'application/json',
            'X-Mongoose-Signature' => $sig,
            'X-Site-Domain'        => '{site_domain}',
        ],
        'timeout'  => 60,
        'blocking' => true,
    ] );
    if ( is_wp_error( $resp ) ) return array( 'ok' => false, 'error' => $resp->get_error_message() );
    $code = (int) wp_remote_retrieve_response_code( $resp );
    $data = json_decode( wp_remote_retrieve_body( $resp ), true );
    if ( ! is_array( $data ) ) return array( 'ok' => false, 'error' => 'bad response (HTTP ' . $code . ')' );
    return $data;
}}

// ── Seller Packs (LPE1) page ────────────────────────────────────────────────
// First-stage reply to an LPE1 request: the branded cover letter plus the statutory admin-charge
// summary, generated live by Mongoose and returned as printable PDF links. Mongoose sends nothing and
// never releases the completed form here (that is Stage 2, only after the fee is paid).
function pc_rtm_seller_pack_page() {{
    $result = null; $err = '';
    if ( isset( $_POST['pc_rtm_sp_go'] ) && check_admin_referer( 'pc_rtm_seller_pack' ) ):
        $flat = sanitize_text_field( $_POST['sp_flat'] ?? '' );
        if ( $flat === '' ):
            $err = 'Enter a flat first.';
        else:
            $payload = array( 'flat' => $flat );
            $map = array( 'seller_name' => 'sp_seller', 'conveyancer_firm' => 'sp_firm', 'conveyancer_contact' => 'sp_contact', 'conveyancer_email' => 'sp_email', 'their_ref' => 'sp_their_ref', 'request_date' => 'sp_req_date' );
            foreach ( $map as $k => $f ):
                $v = sanitize_text_field( $_POST[ $f ] ?? '' );
                if ( $v !== '' ) $payload[ $k ] = $v;
            endforeach;
            if ( ! empty( $_POST['sp_save'] ) ) $payload['save'] = true;
            $result = pc_rtm_call_mongoose( 'seller_pack_first_stage', $payload );
            if ( $result === null ) $err = 'Mongoose connection is not configured. Set the Webhook URL and secret under Settings.';
        endif;
    endif;
    ?>
    <div class="wrap">
    <h1>Seller packs (LPE1)</h1>
    <p>Generate the first-stage reply to an LPE1 request: the branded cover letter plus the statutory administration-charge summary, ready to print or attach. Mongoose sends nothing and does not release the completed form until the fee is paid.</p>
    <?php if ( $err ): ?><div class="notice notice-error"><p><?php echo esc_html( $err ); ?></p></div><?php endif; ?>
    <form method="post">
    <?php wp_nonce_field( 'pc_rtm_seller_pack' ); ?>
    <table class="form-table">
      <tr><th>Flat</th><td><input name="sp_flat" class="regular-text" placeholder="7 or {example_flat}" value="<?php echo esc_attr( $_POST['sp_flat'] ?? '' ); ?>" required /></td></tr>
      <tr><th>Seller name</th><td><input name="sp_seller" class="regular-text" placeholder="Miss Jane Doe" value="<?php echo esc_attr( $_POST['sp_seller'] ?? '' ); ?>" /></td></tr>
      <tr><th>Conveyancer firm</th><td><input name="sp_firm" class="regular-text" value="<?php echo esc_attr( $_POST['sp_firm'] ?? '' ); ?>" /></td></tr>
      <tr><th>Contact (FAO)</th><td><input name="sp_contact" class="regular-text" value="<?php echo esc_attr( $_POST['sp_contact'] ?? '' ); ?>" /></td></tr>
      <tr><th>Conveyancer email</th><td><input name="sp_email" type="email" class="regular-text" value="<?php echo esc_attr( $_POST['sp_email'] ?? '' ); ?>" /></td></tr>
      <tr><th>Their reference</th><td><input name="sp_their_ref" class="regular-text" value="<?php echo esc_attr( $_POST['sp_their_ref'] ?? '' ); ?>" /></td></tr>
      <tr><th>Request date</th><td><input name="sp_req_date" class="regular-text" placeholder="YYYY-MM-DD" value="<?php echo esc_attr( $_POST['sp_req_date'] ?? '' ); ?>" /></td></tr>
      <tr><th></th><td><label><input type="checkbox" name="sp_save" value="1" /> Remember these details on the job</label></td></tr>
    </table>
    <p><button type="submit" name="pc_rtm_sp_go" class="button button-primary">Generate first-stage response</button></p>
    </form>
    <?php if ( is_array( $result ) && ! empty( $result['ok'] ) ): ?>
      <hr />
      <h2><?php echo esc_html( $result['premises'] ?? '' ); ?></h2>
      <p><strong>Ref:</strong> <?php echo esc_html( $result['our_ref'] ?? '' ); ?> &middot; <strong>Fee:</strong> <?php echo esc_html( $result['fee'] ?? '' ); ?></p>
      <p>
      <?php if ( ! empty( $result['cover_letter']['url'] ) ): ?>
        <a href="<?php echo esc_url( $result['cover_letter']['url'] ); ?>" class="button button-primary" target="_blank">Cover letter (PDF)</a>
      <?php endif; ?>
      <?php if ( ! empty( $result['admin_charge_summary']['url'] ) ): ?>
        <a href="<?php echo esc_url( $result['admin_charge_summary']['url'] ); ?>" class="button" target="_blank">Admin-charge summary (PDF)</a>
      <?php endif; ?>
      </p>
      <p class="description">Tap a document to open it in a new tab, then print or save it. Links are private and expire.</p>
      <?php if ( ! empty( $result['flags'] ) ): ?>
        <div class="notice notice-warning inline"><p><strong>Fix before issue:</strong></p><ul style="list-style:disc;margin-left:20px">
        <?php foreach ( $result['flags'] as $f ): ?><li><?php echo esc_html( $f ); ?></li><?php endforeach; ?>
        </ul></div>
      <?php endif; ?>
      <?php if ( ! empty( $result['before_issue'] ) ): ?>
        <ul style="list-style:disc;margin-left:20px;color:#666">
        <?php foreach ( $result['before_issue'] as $f ): ?><li><?php echo esc_html( $f ); ?></li><?php endforeach; ?>
        </ul>
      <?php endif; ?>
    <?php elseif ( is_array( $result ) ): ?>
      <div class="notice notice-error"><p>Could not generate: <?php echo esc_html( $result['error'] ?? ( $result['reason'] ?? 'unknown' ) ); ?></p></div>
    <?php endif; ?>
    </div>
    <?php
}}

// ── REST: build verification ──────────────────────────────────────────────────
// Mongoose polls this after a deploy to confirm the site, the zip and the brain agree on the
// version. Read-only, no secrets, safe to expose.
add_action( 'rest_api_init', function () {{
    register_rest_route( 'mongoose/v1', '/build', array(
        'methods' => 'GET',
        'permission_callback' => '__return_true',
        'callback' => function () {{
            return array(
                'ok'      => true,
                'plugin'  => '{plugin_slug}',
                'version' => PC_RTM_VERSION,
                'site'    => get_bloginfo( 'url' ),
            );
        }},
    ) );
}} );

// ── Hook: notify Mongoose when a post is published ────────────────────────────
add_action( 'publish_post', 'pc_rtm_on_publish', 10, 2 );
function pc_rtm_on_publish( int $post_id, WP_Post $post ): void {{
    if ( $post->post_type !== 'post' ) return;
    pc_rtm_send_to_mongoose( 'page_published', [
        'post_id'    => $post_id,
        'post_title' => $post->post_title,
        'post_url'   => get_permalink( $post_id ),
    ] );
}}

// ── Shortcode: leaseholder query form that sends to Mongoose ─────────────────
add_shortcode( 'rtm_contact', 'pc_rtm_contact_shortcode' );
function pc_rtm_contact_shortcode( array $atts ): string {{
    if ( isset( $_POST['pc_rtm_contact_submit'] ) && check_admin_referer( 'pc_rtm_contact', 'pc_rtm_nonce' ) ) {{
        $name    = sanitize_text_field( $_POST['rtm_name'] ?? '' );
        $email   = sanitize_email( $_POST['rtm_email'] ?? '' );
        $message = sanitize_textarea_field( $_POST['rtm_message'] ?? '' );
        if ( $name && $email && $message ) {{
            pc_rtm_send_to_mongoose( 'rtm_query', [
                'name' => $name, 'email' => $email, 'message' => $message,
            ] );
            return '<div class="pc-notice">Thank you: your message has been received by the RTM.</div>';
        }}
    }}
    ob_start(); ?>
    <form method="post" class="pc-contact-form">
    <?php wp_nonce_field( 'pc_rtm_contact', 'pc_rtm_nonce' ); ?>
    <p><label>Name<br><input name="rtm_name" required class="widefat" /></label></p>
    <p><label>Email<br><input name="rtm_email" type="email" required class="widefat" /></label></p>
    <p><label>Message<br><textarea name="rtm_message" rows="5" required class="widefat"></textarea></label></p>
    <p><button type="submit" name="pc_rtm_contact_submit" class="button button-primary">Send to RTM</button></p>
    </form>
    <?php return ob_get_clean();
}}

register_activation_hook( __FILE__, 'pc_rtm_activate' );
function pc_rtm_activate() {{
    add_option( 'pc_rtm_version', PC_RTM_VERSION );
    // Tell Mongoose which version is now live, so its RTM screen confirms the site, the deploy and the
    // brain all agree (turns the sync strip green). Non-blocking; silent if the webhook is not set up.
    pc_rtm_send_to_mongoose( 'deployment_confirmed', [ 'version' => PC_RTM_VERSION ] );
}}

register_deactivation_hook( __FILE__, 'pc_rtm_deactivate' );
function pc_rtm_deactivate() {{
    // Clean up transients etc: options preserved across deactivations
}}
'''


_PORTAL_SYNC_PHP = "<?php\n/** {{rtm.name}} RTM - portal sync API (pairs with Mongoose rtm_portal). */\nif ( ! defined( 'ABSPATH' ) ) exit;\n\n// -- Storage ------------------------------------------------------------------\nregister_activation_hook( PC_RTM_PLUGIN_DIR . '{{wordpress.plugin_slug}}.php', 'pc_rtm_portal_tables' );\nadd_action( 'plugins_loaded', 'pc_rtm_portal_tables' );\nfunction pc_rtm_portal_tables() {\n    global $wpdb;\n    if ( get_option( 'pc_rtm_portal_db_version' ) === '9' ) return;\n    require_once ABSPATH . 'wp-admin/includes/upgrade.php';\n    $charset = $wpdb->get_charset_collate();\n    dbDelta( \"CREATE TABLE {$wpdb->prefix}pc_rtm_claims (\n        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n        encrypted_payload_b64 LONGTEXT NOT NULL,\n        status VARCHAR(40) NOT NULL DEFAULT 'pending',\n        flat_id VARCHAR(20) DEFAULT '',\n        decision VARCHAR(60) DEFAULT '',\n        failure_message TEXT,\n        created_at DATETIME NOT NULL,\n        decided_at DATETIME NULL,\n        PRIMARY KEY (id), KEY status (status)\n    ) $charset;\" );\n    dbDelta( \"CREATE TABLE {$wpdb->prefix}pc_rtm_issues (\n        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n        flat_id VARCHAR(20) NOT NULL DEFAULT '',\n        category VARCHAR(20) NOT NULL DEFAULT 'other',\n        title TEXT, description LONGTEXT,\n        urgency VARCHAR(20) DEFAULT 'routine',\n        location TEXT, payment_date VARCHAR(20) DEFAULT NULL,\n        amount_paid VARCHAR(20) DEFAULT NULL, reference_used VARCHAR(80) DEFAULT NULL,\n        synced TINYINT NOT NULL DEFAULT 0,\n        created_at DATETIME NOT NULL,\n        PRIMARY KEY (id), KEY synced (synced)\n    ) $charset;\" );\n    // Demand PDFs pushed by Mongoose (reconciled from the Pro build). content_b64 is the PDF the\n    // leaseholder downloads; flat_id gates who may fetch it. Sensitive strategy PDFs never come here.\n    dbDelta( \"CREATE TABLE {$wpdb->prefix}pc_rtm_pdfs (\n        pdf_key VARCHAR(120) NOT NULL,\n        document_number VARCHAR(120) NOT NULL DEFAULT '',\n        flat_id VARCHAR(20) NOT NULL DEFAULT '',\n        mime VARCHAR(120) NOT NULL DEFAULT 'application/pdf',\n        content_b64 LONGTEXT NOT NULL,\n        sha256 VARCHAR(100) DEFAULT '',\n        created_at DATETIME NOT NULL,\n        PRIMARY KEY (pdf_key), KEY document_number (document_number), KEY flat_id (flat_id)\n    ) $charset;\" );\n    // Member vote casts: one row per (vote_id, flat_id). A re-cast updates the same row so a flat is\n    // never counted twice. Mongoose PULLS pending rows (synced=0) and re-checks eligibility on ingest.\n    dbDelta( \"CREATE TABLE {$wpdb->prefix}pc_rtm_vote_casts (\n        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n        vote_id BIGINT UNSIGNED NOT NULL,\n        flat_id VARCHAR(20) NOT NULL DEFAULT '',\n        choice TEXT NOT NULL,\n        synced TINYINT NOT NULL DEFAULT 0,\n        cast_at DATETIME NOT NULL,\n        PRIMARY KEY (id), UNIQUE KEY vote_flat (vote_id, flat_id), KEY synced (synced)\n    ) $charset;\" );\n    // Director-requested RTM-membership changes. WordPress is the toggle surface; Mongoose (the ledger\n    // and source of truth) PULLS pending changes, applies them, and pushes confirmed member_flat_ids back\n    // in the next snapshot, so the WP toggle exists without WP becoming a second source of truth.\n    // H4: queue rows carry a recoverable lifecycle. op_key is a per-intent idempotency key (dedupe of\n    // contradictory/duplicate pending rows is enforced in pc_rtm_queue_enqueue). status extends beyond\n    // pending/applied to cancelled (director recovery) and dead_letter. attempts/last_error/lease_until\n    // are populated by the applying side (Mongoose) and surfaced for operability.\n    dbDelta( \"CREATE TABLE {$wpdb->prefix}pc_rtm_membership_changes (\n        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n        flat_id VARCHAR(20) NOT NULL DEFAULT '',\n        requested_member TINYINT NOT NULL DEFAULT 0,\n        requested_by VARCHAR(190) NOT NULL DEFAULT '',\n        requested_at DATETIME NOT NULL,\n        status VARCHAR(20) NOT NULL DEFAULT 'pending',\n        op_key VARCHAR(64) NOT NULL DEFAULT '',\n        attempts INT NOT NULL DEFAULT 0,\n        last_error TEXT,\n        lease_until DATETIME NULL,\n        applied_at DATETIME NULL,\n        PRIMARY KEY (id), KEY status (status), KEY flat_id (flat_id), KEY op_key (op_key)\n    ) $charset;\" );\n    // A director \"mark paid\" queued in WordPress for Mongoose (the ledger) to pull, apply and confirm on\n    // the next snapshot , WordPress never touches the ledger itself (CGNAT: WP cannot push to Mongoose).\n    dbDelta( \"CREATE TABLE {$wpdb->prefix}pc_rtm_payment_marks (\n        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n        flat_id VARCHAR(20) NOT NULL DEFAULT '',\n        document_number VARCHAR(120) NOT NULL DEFAULT '',\n        amount VARCHAR(40) NOT NULL DEFAULT '',\n        received_date VARCHAR(20) NOT NULL DEFAULT '',\n        requested_by VARCHAR(190) NOT NULL DEFAULT '',\n        requested_at DATETIME NOT NULL,\n        status VARCHAR(20) NOT NULL DEFAULT 'pending',\n        op_key VARCHAR(64) NOT NULL DEFAULT '',\n        attempts INT NOT NULL DEFAULT 0,\n        last_error TEXT,\n        lease_until DATETIME NULL,\n        applied_at DATETIME NULL,\n        PRIMARY KEY (id), KEY status (status), KEY flat_id (flat_id), KEY op_key (op_key)\n    ) $charset;\" );\n    // A director \"generate this missing document\" request queued for Mongoose to pull, build (a receipt\n    // pack or a demand PDF) and push back on the next snapshot , WordPress cannot generate documents and\n    // cannot reach Mongoose (CGNAT), so it only queues and then watches the snapshot for the result.\n    dbDelta( \"CREATE TABLE {$wpdb->prefix}pc_rtm_doc_requests (\n        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n        flat_id VARCHAR(20) NOT NULL DEFAULT '',\n        document_number VARCHAR(120) NOT NULL DEFAULT '',\n        doc_type VARCHAR(20) NOT NULL DEFAULT 'receipt',\n        requested_by VARCHAR(190) NOT NULL DEFAULT '',\n        requested_at DATETIME NOT NULL,\n        status VARCHAR(20) NOT NULL DEFAULT 'pending',\n        op_key VARCHAR(64) NOT NULL DEFAULT '',\n        attempts INT NOT NULL DEFAULT 0,\n        last_error TEXT,\n        lease_until DATETIME NULL,\n        applied_at DATETIME NULL,\n        PRIMARY KEY (id), KEY status (status), KEY flat_id (flat_id), KEY op_key (op_key)\n    ) $charset;\" );\n    // Durable, inspectable audit log of every demand/receipt email the plugin sends (one row per send\n    // attempt). Written by pc_rtm_record_send() right after wp_mail returns, so the director can see exactly\n    // what went out and whether it was accepted for delivery. Read-only viewer: admin \"Sent mail log\" page.\n    dbDelta( \"CREATE TABLE {$wpdb->prefix}pc_rtm_send_log (\n        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n        kind VARCHAR(20) NOT NULL DEFAULT '',\n        flat_id VARCHAR(20) NOT NULL DEFAULT '',\n        document_number VARCHAR(120) NOT NULL DEFAULT '',\n        to_email VARCHAR(190) NOT NULL DEFAULT '',\n        subject TEXT,\n        result VARCHAR(12) NOT NULL DEFAULT '',\n        error TEXT,\n        sent_at DATETIME NOT NULL,\n        sent_by VARCHAR(60) NOT NULL DEFAULT '',\n        PRIMARY KEY (id), KEY sent_at (sent_at), KEY flat_id (flat_id)\n    ) $charset;\" );\n    update_option( 'pc_rtm_portal_db_version', '9' );\n}\n\n/**\n * Record one demand/receipt send attempt in the durable audit log (pc_rtm_send_log). Called by the send\n * routes right after the wp_mail result is known, recording the real outcome: 'sent' when wp_mail returned\n * true, 'failed' otherwise (with the error reason). This MUST NEVER break or alter the send path: it is\n * fully guarded so a logging failure cannot turn a real send into a failure or vice versa, and it only ever\n * observes the outcome, it never changes what the route returns.\n *\n * @param string $kind  'demand' or 'receipt'.\n * @param array  $args  Send facts: flat_id, document_number, to_email, subject, sent_by.\n * @param bool   $ok    True when wp_mail accepted the message for delivery.\n * @param string $error Failure reason when $ok is false (empty on success).\n */\nfunction pc_rtm_record_send( $kind, $args, $ok, $error = '' ) {\n    try {\n        global $wpdb;\n        if ( ! is_array( $args ) ) $args = array();\n        $wpdb->insert( $wpdb->prefix . 'pc_rtm_send_log', array(\n            'kind'            => substr( sanitize_key( (string) $kind ), 0, 20 ),\n            'flat_id'         => substr( sanitize_text_field( (string) ( $args['flat_id'] ?? '' ) ), 0, 20 ),\n            'document_number' => substr( sanitize_text_field( (string) ( $args['document_number'] ?? '' ) ), 0, 120 ),\n            'to_email'        => substr( sanitize_text_field( (string) ( $args['to_email'] ?? '' ) ), 0, 190 ),\n            'subject'         => (string) ( $args['subject'] ?? '' ),\n            'result'          => $ok ? 'sent' : 'failed',\n            'error'           => $ok ? '' : (string) $error,\n            'sent_at'         => gmdate( 'Y-m-d H:i:s' ),\n            'sent_by'         => substr( sanitize_text_field( (string) ( $args['sent_by'] ?? 'mongoose' ) ), 0, 60 ),\n        ) );\n    } catch ( \\Throwable $e ) {\n        // Never let an audit-log write affect the send path or its return value.\n    }\n}\n\n// -- Sent mail log (read-only admin viewer) -------------------------------------\n// A read-only sibling admin page under the RTM menu: the director inspects exactly what demand/receipt\n// email went out (newest first). No destructive actions, just a viewer, with an optional result filter.\nadd_action( 'admin_menu', function () {\n    add_submenu_page( '{{wordpress.plugin_slug}}', 'Sent mail log', 'Sent mail log',\n        'rtm_manage_portal', 'pc-rtm-send-log', 'pc_rtm_send_log_page' );\n}, 20 );\n\nfunction pc_rtm_send_log_page() {\n    if ( ! ( current_user_can( 'rtm_manage_portal' ) || current_user_can( 'manage_options' ) ) ) {\n        wp_die( esc_html__( 'You do not have permission to view the sent mail log.', '{{wordpress.plugin_slug}}' ) );\n    }\n    global $wpdb;\n    $table = $wpdb->prefix . 'pc_rtm_send_log';\n    // Optional filter by result ('sent' or 'failed'); anything else means \"all\".\n    $filter = isset( $_GET['result'] ) ? sanitize_key( (string) $_GET['result'] ) : '';\n    if ( $filter === 'sent' || $filter === 'failed' ) {\n        $rows = $wpdb->get_results( $wpdb->prepare(\n            \"SELECT * FROM $table WHERE result=%s ORDER BY id DESC LIMIT 200\", $filter ), ARRAY_A );\n    } else {\n        $filter = '';\n        $rows = $wpdb->get_results( \"SELECT * FROM $table ORDER BY id DESC LIMIT 200\", ARRAY_A );\n    }\n    $rows = is_array( $rows ) ? $rows : array();\n    $base = admin_url( 'admin.php?page=pc-rtm-send-log' );\n\n    echo '<div class=\"wrap\"><h1>' . esc_html__( 'Sent mail log', '{{wordpress.plugin_slug}}' ) . '</h1>';\n    echo '<p class=\"description\">' . esc_html__( 'Every demand and receipt email the plugin sent, newest first (up to 200). Read-only. Times are UTC.', '{{wordpress.plugin_slug}}' ) . '</p>';\n    echo '<p>';\n    foreach ( array( '' => __( 'All', '{{wordpress.plugin_slug}}' ), 'sent' => __( 'Sent', '{{wordpress.plugin_slug}}' ), 'failed' => __( 'Failed', '{{wordpress.plugin_slug}}' ) ) as $key => $label ) {\n        $url = $key === '' ? $base : add_query_arg( 'result', $key, $base );\n        if ( $filter === $key ) {\n            echo '<strong>' . esc_html( $label ) . '</strong>';\n        } else {\n            echo '<a href=\"' . esc_url( $url ) . '\">' . esc_html( $label ) . '</a>';\n        }\n        echo ' ';\n    }\n    echo '</p>';\n\n    echo '<table class=\"widefat striped\"><thead><tr>'\n        . '<th>' . esc_html__( 'Sent at (UTC)', '{{wordpress.plugin_slug}}' ) . '</th>'\n        . '<th>' . esc_html__( 'Kind', '{{wordpress.plugin_slug}}' ) . '</th>'\n        . '<th>' . esc_html__( 'Flat', '{{wordpress.plugin_slug}}' ) . '</th>'\n        . '<th>' . esc_html__( 'Document', '{{wordpress.plugin_slug}}' ) . '</th>'\n        . '<th>' . esc_html__( 'To', '{{wordpress.plugin_slug}}' ) . '</th>'\n        . '<th>' . esc_html__( 'Subject', '{{wordpress.plugin_slug}}' ) . '</th>'\n        . '<th>' . esc_html__( 'Result', '{{wordpress.plugin_slug}}' ) . '</th>'\n        . '<th>' . esc_html__( 'Error', '{{wordpress.plugin_slug}}' ) . '</th>'\n        . '</tr></thead><tbody>';\n    if ( empty( $rows ) ) {\n        echo '<tr><td colspan=\"8\">' . esc_html__( 'No sends recorded yet.', '{{wordpress.plugin_slug}}' ) . '</td></tr>';\n    } else {\n        foreach ( $rows as $r ) {\n            echo '<tr>'\n                . '<td>' . esc_html( (string) ( $r['sent_at'] ?? '' ) ) . '</td>'\n                . '<td>' . esc_html( (string) ( $r['kind'] ?? '' ) ) . '</td>'\n                . '<td>' . esc_html( (string) ( $r['flat_id'] ?? '' ) ) . '</td>'\n                . '<td>' . esc_html( (string) ( $r['document_number'] ?? '' ) ) . '</td>'\n                . '<td>' . esc_html( (string) ( $r['to_email'] ?? '' ) ) . '</td>'\n                . '<td>' . esc_html( (string) ( $r['subject'] ?? '' ) ) . '</td>'\n                . '<td>' . esc_html( (string) ( $r['result'] ?? '' ) ) . '</td>'\n                . '<td>' . esc_html( (string) ( $r['error'] ?? '' ) ) . '</td>'\n                . '</tr>';\n        }\n    }\n    echo '</tbody></table></div>';\n}\n\n// -- Helpers --------------------------------------------------------------------\nfunction pc_rtm_admin_permission() { return current_user_can( 'rtm_manage_portal' ) || current_user_can( 'manage_options' ); }\n\n/** Per-IP throttle for the public claim form: at most 8 STORED claims per IP per hour (only successful,\n * validated submissions count, so typos never lock anyone out and a couple behind one NAT are fine), so a\n * scripted run still cannot flood the claims table or the director's inbox. Returns true when over the\n * limit (and increments the window counter as a side effect when under it). */\nfunction pc_rtm_claim_rl_key() {\n    $ip = isset( $_SERVER['REMOTE_ADDR'] ) ? preg_replace( '/[^0-9a-fA-F:.]/', '', (string) $_SERVER['REMOTE_ADDR'] ) : '';\n    return $ip === '' ? '' : 'pc_rtm_claim_rl_' . md5( $ip );\n}\nfunction pc_rtm_claim_rate_limited() {\n    // Read-only: never increments. The counter only moves when a claim is actually stored\n    // (pc_rtm_claim_record_stored), so a genuine user fixing a typo, a failed insert, or a failed\n    // encryption never burns quota, exactly as the docstring promises.\n    $key = pc_rtm_claim_rl_key();\n    if ( $key === '' ) return false;\n    return (int) get_transient( $key ) >= 8;\n}\nfunction pc_rtm_claim_record_stored() {\n    // Call ONLY after a claim was genuinely committed (instant provision or queued insert succeeded).\n    $key = pc_rtm_claim_rl_key();\n    if ( $key === '' ) return;\n    set_transient( $key, (int) get_transient( $key ) + 1, HOUR_IN_SECONDS );\n}\n\n// \u2500\u2500 Queue lifecycle + dedupe + recovery (H4) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n// The membership / payment-mark / doc-request tables are WordPress-side COMMAND queues that Mongoose (the\n// ledger) pulls, applies and acks. The audit found they had only pending/applied, no operation id, attempt,\n// lease, error, cancel or dead-letter, so contradictory pending rows piled up (rapid membership on/off),\n// repeated doc-gen clicks stacked, and one stuck request made a whole area look \"in progress\" with no\n// recovery. These helpers add: one current command per entity+intent (dedupe), a per-intent operation key\n// (idempotent double-submit), lifecycle states, and per-item + oldest-pending-age surfacing.\n\n/** A stable per-intent operation key. Identical intent (same salient fields) -> same key, so a double\n *  click / retry collapses to ONE pending row rather than stacking. */\nfunction pc_rtm_operation_key_for( $intent, array $parts ) {\n    return substr( hash( 'sha256', (string) $intent . '|' . implode( '|', array_map( 'strval', $parts ) ) ), 0, 48 );\n}\n\n/** True once the H4 lifecycle columns exist on a queue table (dbDelta upgrade to db_version 8 has run). */\nfunction pc_rtm_queue_has_lifecycle( $table ) {\n    global $wpdb;\n    static $cache = array();\n    if ( array_key_exists( $table, $cache ) ) return $cache[ $table ];\n    $col = $wpdb->get_var( $wpdb->prepare( \"SHOW COLUMNS FROM `$table` LIKE %s\", 'op_key' ) );\n    return $cache[ $table ] = ( $col === 'op_key' );\n}\n\n/**\n * Insert a queue COMMAND with dedupe. Returns array( ok, id, op_key, deduped ).\n *   1. Idempotency: if a PENDING row already carries this op_key, that IS this command , return it, do not\n *      insert a duplicate (defeats double-submit / a lost-response retry).\n *   2. Contradictory-intent collapse: cancel any prior PENDING rows matching $cancel_pending_where (e.g. the\n *      same flat_id) so at most the LATEST intent remains pending for Mongoose (rapid on/off -> latest wins).\n *   3. Insert the new pending row (with op_key + attempts=0 when the lifecycle columns exist).\n * Degrades safely on a pre-upgrade table (no op_key column): inserts without dedupe rather than erroring.\n */\nfunction pc_rtm_queue_enqueue( $table, array $row, $op_key, array $cancel_pending_where = array() ) {\n    global $wpdb;\n    $now    = current_time( 'mysql', true );\n    $has_lc = pc_rtm_queue_has_lifecycle( $table );\n    if ( $has_lc && $op_key !== '' ) {\n        $existing = $wpdb->get_var( $wpdb->prepare(\n            \"SELECT id FROM `$table` WHERE op_key=%s AND status='pending' ORDER BY id DESC LIMIT 1\", $op_key ) );\n        if ( $existing ) return array( 'ok' => true, 'id' => (int) $existing, 'op_key' => $op_key, 'deduped' => true );\n    }\n    if ( $has_lc && $cancel_pending_where ) {\n        $where_sql = array(); $vals = array();\n        foreach ( $cancel_pending_where as $col => $val ) { $where_sql[] = \"`$col`=%s\"; $vals[] = (string) $val; }\n        $sql = \"UPDATE `$table` SET status='cancelled', last_error=%s, applied_at=%s WHERE status='pending' AND \" . implode( ' AND ', $where_sql );\n        array_unshift( $vals, 'superseded by a newer request', $now );\n        $wpdb->query( $wpdb->prepare( $sql, $vals ) );\n    }\n    if ( $has_lc ) { $row['op_key'] = $op_key; $row['attempts'] = 0; }\n    $row['status'] = 'pending';\n    if ( empty( $row['requested_at'] ) ) $row['requested_at'] = $now;\n    $ins = $wpdb->insert( $table, $row );\n    if ( $ins === false ) return array( 'ok' => false, 'id' => 0, 'op_key' => $op_key, 'deduped' => false );\n    return array( 'ok' => true, 'id' => (int) $wpdb->insert_id, 'op_key' => $op_key, 'deduped' => false );\n}\n\n/** Operability snapshot for a queue table: pending count, oldest-pending age (seconds) + timestamp, and\n *  error / dead-letter counts. Replaces a single global count so one stuck item cannot make a whole area\n *  read as uniformly \"in progress\". */\nfunction pc_rtm_queue_status( $table ) {\n    global $wpdb;\n    $out = array( 'exists' => false, 'pending' => 0, 'oldest_pending_age' => 0, 'oldest_pending_at' => '', 'dead_letter' => 0, 'errored' => 0 );\n    if ( $wpdb->get_var( $wpdb->prepare( 'SHOW TABLES LIKE %s', $table ) ) !== $table ) return $out;\n    $out['exists']  = true;\n    $out['pending'] = (int) $wpdb->get_var( \"SELECT COUNT(*) FROM `$table` WHERE status='pending'\" );\n    $oldest = $wpdb->get_var( \"SELECT requested_at FROM `$table` WHERE status='pending' ORDER BY requested_at ASC LIMIT 1\" );\n    if ( $oldest ) {\n        $out['oldest_pending_at'] = (string) $oldest;\n        $ts = strtotime( $oldest . ' UTC' );\n        if ( $ts ) $out['oldest_pending_age'] = max( 0, time() - $ts );\n    }\n    if ( pc_rtm_queue_has_lifecycle( $table ) ) {\n        $out['dead_letter'] = (int) $wpdb->get_var( \"SELECT COUNT(*) FROM `$table` WHERE status='dead_letter'\" );\n        $out['errored']     = (int) $wpdb->get_var( \"SELECT COUNT(*) FROM `$table` WHERE status='pending' AND last_error IS NOT NULL AND last_error<>''\" );\n    }\n    return $out;\n}\n\n/** The queue-key => base table map for the recovery handler (code-controlled table names). */\nfunction pc_rtm_recoverable_queues() {\n    global $wpdb;\n    return array(\n        'membership' => $wpdb->prefix . 'pc_rtm_membership_changes',\n        'payment'    => $wpdb->prefix . 'pc_rtm_payment_marks',\n        'doc'        => $wpdb->prefix . 'pc_rtm_doc_requests',\n    );\n}\n\n// Admin recovery: cancel a single STUCK pending command before Mongoose applies it. Gated on the same\n// capability as the queue-producing actions; only a still-pending row can be cancelled (idempotent).\nadd_action( 'admin_post_pc_rtm_cancel_queued', 'pc_rtm_handle_cancel_queued' );\nfunction pc_rtm_handle_cancel_queued() {\n    if ( ! pc_rtm_admin_permission() ) {\n        wp_die( esc_html__( 'You do not have permission to cancel queued operations.', '{{wordpress.plugin_slug}}' ) );\n    }\n    $queue = sanitize_key( wp_unslash( $_POST['queue'] ?? '' ) );\n    $id    = (int) ( $_POST['queue_id'] ?? 0 );\n    check_admin_referer( 'pc_rtm_cancel_queued_' . $queue . '_' . $id );\n    $queues = pc_rtm_recoverable_queues();\n    $ok = false;\n    if ( isset( $queues[ $queue ] ) && $id > 0 ) {\n        global $wpdb;\n        $table = $queues[ $queue ];\n        $set = array( 'status' => 'cancelled' );\n        if ( pc_rtm_queue_has_lifecycle( $table ) ) {\n            $set['last_error']  = 'cancelled by ' . wp_get_current_user()->user_login;\n            $set['applied_at']  = current_time( 'mysql', true );\n        }\n        $ok = (bool) $wpdb->update( $table, $set, array( 'id' => $id, 'status' => 'pending' ) );\n    }\n    $ref  = wp_get_referer();\n    $base = $ref ? $ref : admin_url( 'admin.php?page=pc-rtm-charges' );\n    wp_safe_redirect( add_query_arg( $ok ? array( 'queue_cancelled' => 1 ) : array( 'queue_cancel_error' => 1 ), $base ) );\n    exit;\n}\n\nfunction pc_rtm_ip_allowed() {\n    $allow = trim( (string) get_option( \"pc_rtm_allowed_ips\", \"\" ) );\n    if ( $allow === \"\" ) return true;\n    $ip = isset( $_SERVER[\"REMOTE_ADDR\"] ) ? $_SERVER[\"REMOTE_ADDR\"] : \"\";\n    foreach ( array_map( \"trim\", explode( \",\", $allow ) ) as $a ) {\n        if ( $a !== \"\" && $a === $ip ) return true;\n    }\n    return false;\n}\n\nfunction pc_rtm_verify_write( WP_REST_Request $req ) {\n    if ( ! pc_rtm_ip_allowed() ) return new WP_Error( \"ip_denied\", \"Source address not allowed.\", array( \"status\" => 403 ) );\n    $pub = trim( (string) get_option( \"pc_rtm_snapshot_pubkey\", \"\" ) );\n    if ( $pub !== \"\" && function_exists( \"sodium_crypto_sign_verify_detached\" ) ) {\n        $sig = (string) $req->get_header( \"X-Mongoose-Ed25519\" );\n        $raw = $sig !== \"\" ? base64_decode( $sig, true ) : false;\n        $key = base64_decode( $pub, true );\n        if ( $raw === false || $key === false || strlen( $key ) !== 32 ) {\n            return new WP_Error( \"no_signature\", \"Missing or malformed Ed25519 signature.\", array( \"status\" => 403 ) );\n        }\n        if ( ! sodium_crypto_sign_verify_detached( $raw, $req->get_body(), $key ) ) {\n            return new WP_Error( \"bad_signature\", \"Ed25519 signature invalid; write refused.\", array( \"status\" => 403 ) );\n        }\n        return true;\n    }\n    return pc_rtm_verify_hmac( $req );\n}\n\nfunction pc_rtm_verify_hmac( WP_REST_Request $req ) {\n    $secret = (string) get_option( 'pc_rtm_webhook_secret', '' );\n    if ( $secret === '' ) return new WP_Error( 'no_secret', 'Set the Mongoose webhook secret in RTM Settings first.', array( 'status' => 403 ) );\n    $sig = trim( (string) $req->get_header( 'X-Mongoose-Signature' ) );\n    $given = preg_replace( '/^sha256=/i', '', $sig );\n    $body = (string) $req->get_body();\n    $ts = trim( (string) $req->get_header( 'X-Mongoose-Timestamp' ) );\n\n    if ( $ts !== '' ) {\n        // B3: the timestamp is AUTH FRESHNESS ONLY (reject stale / replayed-late signatures), NEVER the\n        // business-operation identity. The old code wrote a \"seen\" transient BEFORE the route's write and\n        // returned already_applied:true on repeat, which could BOTH suppress an operation that crashed\n        // before completing AND let a freshly-signed retry REPEAT one that did complete. Instead:\n        //   - the non-idempotent, side-effectful routes (invite / announce / send-receipt / claim-notify)\n        //     wrap their work in pc_rtm_run_once(), keyed on a caller-supplied durable operation id\n        //     (X-Mongoose-Operation-Id, or body operation_id, falling back to the signature). run_once\n        //     records completion AFTER the side effect succeeds and replays the stored original result on a\n        //     retry, so a crash-before-side-effect stays retryable and a completed op is never repeated;\n        //   - every other route here (snapshot overwrite, the *-ack retirements, demand-pdf REPLACE,\n        //     director-summary overwrite) is idempotent by construction, so a repeat re-applies the same\n        //     state safely and needs no suppression at the auth layer.\n        if ( ! ctype_digit( $ts ) || abs( time() - (int) $ts ) > 300 ) {\n            return new WP_Error( 'stale_request', 'Request timestamp is outside the allowed window.', array( 'status' => 403 ) );\n        }\n        $calc = hash_hmac( 'sha256', $ts . '.' . $body, $secret );\n        if ( ! $given || ! hash_equals( $calc, $given ) ) {\n            return new WP_Error( 'bad_signature', 'HMAC signature mismatch.', array( 'status' => 403 ) );\n        }\n        return true;\n    }\n\n    // Legacy path (no timestamp header): sign over the body only. Now DISABLED by default: current\n    // Mongoose always sends a timestamped, replay-protected signature, so the only reason to accept a\n    // body-only signature is a temporary version skew during an upgrade. Re-enable for that window only\n    // by setting the option pc_rtm_accept_legacy_hmac to '1'.\n    if ( get_option( 'pc_rtm_accept_legacy_hmac', '0' ) !== '1' ) {\n        return new WP_Error( 'legacy_disabled', 'Timestamped signature required (legacy HMAC disabled).', array( 'status' => 403 ) );\n    }\n    $calc = hash_hmac( 'sha256', $body, $secret );\n    if ( ! $given || ! hash_equals( $calc, $given ) ) {\n        return new WP_Error( 'bad_signature', 'HMAC signature mismatch.', array( 'status' => 403 ) );\n    }\n    return true;\n}\n\n// \u2500\u2500 Operation-level idempotency for non-idempotent side-effectful routes (B3) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n// Business identity for a state-changing request is a caller-supplied DURABLE operation id, stable across\n// legitimate retries (unlike the HMAC timestamp, which changes each retry). Prefer the X-Mongoose-Operation-Id\n// header, then a body `operation_id`, then fall back to the raw signature so an older caller that re-sends a\n// byte-identical request still gets replay suppression. NOTE: a caller that legitimately RE-signs with a\n// fresh timestamp but no operation_id produces a different signature and is (correctly) treated as a new\n// operation, so Mongoose SHOULD send a stable operation_id for the four email routes below to be fully\n// idempotent across re-signs (interface-contract item, tracked for Phase 1).\nfunction pc_rtm_operation_key( WP_REST_Request $req ) {\n    $op = trim( (string) $req->get_header( 'X-Mongoose-Operation-Id' ) );\n    if ( $op === '' ) {\n        $b = $req->get_json_params();\n        if ( is_array( $b ) && ! empty( $b['operation_id'] ) ) $op = (string) $b['operation_id'];\n    }\n    if ( $op === '' ) {\n        $sig = preg_replace( '/^sha256=/i', '', trim( (string) $req->get_header( 'X-Mongoose-Signature' ) ) );\n        $op = 'sig:' . $sig;\n    }\n    return 'pc_rtm_op_' . md5( $op );\n}\n\n/**\n * Run a side-effectful route body AT MOST ONCE per operation id, honestly.\n *   - if the op already COMPLETED, replay the stored ORIGINAL result (with already_applied=true) and do\n *     NOT re-run the side effect;\n *   - otherwise run $fn, and record completion ONLY when it SUCCEEDED (ok result, not a WP_Error / ok:false),\n *     so a failed attempt stays retryable and the completion marker is written AFTER the side effect (fixing\n *     the old \"mark-before-write\" bug).\n * Realistic guarantee: exactly-once in the happy path; a crash strictly between the side effect and the\n * completion write can re-send an email on retry (at-least-once), unavoidable without SMTP-layer keys, and\n * a durable outbox with a processing lease is the Phase 1 upgrade. Failure (WP_Error / ok:false) is never\n * cached, so retries can complete the work.\n */\nfunction pc_rtm_run_once( WP_REST_Request $req, callable $fn ) {\n    $key = pc_rtm_operation_key( $req );\n    $rec = get_transient( $key );\n    if ( is_array( $rec ) && ( $rec['state'] ?? '' ) === 'completed' ) {\n        $orig = $rec['result'] ?? array( 'ok' => true );\n        if ( is_array( $orig ) ) $orig['already_applied'] = true;\n        return $orig;\n    }\n    $result = $fn();\n    $succeeded = ! is_wp_error( $result ) && ( ! is_array( $result ) || ! empty( $result['ok'] ) );\n    if ( $succeeded ) {\n        set_transient( $key, array( 'state' => 'completed', 'result' => $result, 'at' => time() ), 15 * MINUTE_IN_SECONDS );\n    }\n    return $result;\n}\n\n// \u2500\u2500 WordPress-side sign-up matching (against the snapshot Mongoose already pushed) \u2500\u2500\u2500\u2500\u2500\n// The snapshot already carries every flat's payment_reference_display + leaseholder_email and every\n// demand's document_number (the portal displays them), so WordPress can match a sign-up locally and\n// provision instantly, with no async round-trip. Match is STRICT: payment reference AND document number\n// AND the on-file email must all agree (case-insensitive). Returns the flat_id on a full match, else ''.\n// Anything short of a full match returns '' and the caller falls back to the sealed-claim queue that\n// Mongoose / the director handle, so a neighbour who knows the (guessable) reference + document but not\n// the on-file email never gets an instant account.\nfunction pc_rtm_signup_match_flat( $ref, $doc, $email ) {\n    // H2: a STALE snapshot can still hold a former leaseholder's reference + email. Instant, no-review\n    // provisioning must require a snapshot fresh enough to be trusted; if it is stale (Mongoose stopped\n    // publishing, e.g. an ownership change is mid-flight), return '' so the caller falls back to the\n    // sealed-claim queue for a human/Mongoose review , no account is created from stale data.\n    if ( ! pc_rtm_snapshot_is_fresh() ) return '';\n    $snap = json_decode( (string) get_option( 'pc_rtm_snapshot', '' ), true );\n    if ( ! is_array( $snap ) ) return '';\n    $ref   = strtolower( trim( (string) $ref ) );\n    $doc   = strtolower( trim( (string) $doc ) );\n    $email = strtolower( trim( (string) $email ) );\n    if ( $ref === '' || $doc === '' || ! is_email( $email ) ) return '';\n    // 1. Find the flat whose payment reference matches, and read its on-file email.\n    $flat_id = ''; $on_file_email = '';\n    foreach ( (array) ( $snap['flats'] ?? array() ) as $f ) {\n        if ( strtolower( trim( (string) ( $f['payment_reference_display'] ?? '' ) ) ) === $ref ) {\n            $flat_id       = (string) ( $f['flat_id'] ?? '' );\n            $on_file_email = strtolower( trim( (string) ( $f['leaseholder_email'] ?? ( $f['email'] ?? '' ) ) ) );\n            break;\n        }\n    }\n    if ( $flat_id === '' ) return '';\n    // 2. The document number must belong to that same flat.\n    $doc_ok = false;\n    foreach ( (array) ( $snap['demands'] ?? array() ) as $d ) {\n        if ( strtoupper( (string) ( $d['flat_id'] ?? '' ) ) === strtoupper( $flat_id )\n             && strtolower( trim( (string) ( $d['document_number'] ?? '' ) ) ) === $doc ) { $doc_ok = true; break; }\n    }\n    if ( ! $doc_ok ) return '';\n    // 3. The email must be the one on file for that flat (the security gate the owner chose).\n    if ( $on_file_email === '' || $on_file_email !== $email ) return '';\n    return $flat_id;\n}\n\n/** The leaseholder name the snapshot holds for a flat (used only to set the account display name). */\nfunction pc_rtm_signup_flat_name( $flat_id ) {\n    $snap = json_decode( (string) get_option( 'pc_rtm_snapshot', '' ), true );\n    if ( ! is_array( $snap ) ) return '';\n    foreach ( (array) ( $snap['flats'] ?? array() ) as $f ) {\n        if ( strtoupper( (string) ( $f['flat_id'] ?? '' ) ) === strtoupper( (string) $flat_id ) ) {\n            return (string) ( $f['leaseholder_name'] ?? '' );\n        }\n    }\n    return '';\n}\n\n// Normalise a key so camelCase, kebab-case, spaces and mixed case all collapse to snake_case. Without\n// this, \"legalAdvice\" lowercased to \"legaladvice\" and slipped past a \"legal_advice\" denylist entry.\nfunction pc_rtm_normalize_key( $k ) {\n    $s = preg_replace( '/([a-z0-9])([A-Z])/', '$1_$2', (string) $k ); // camelCase -> camel_Case\n    $s = strtolower( (string) $s );\n    $s = preg_replace( '/[^a-z0-9]+/', '_', $s );                     // hyphen/space/etc -> _\n    return trim( (string) $s, '_' );\n}\n\n// The ONLY top-level sections a leaseholder snapshot may contain. This is an ALLOWLIST: anything else\n// (a new sensitive section such as caseTheory / tribunalStrategy / directorCommentary) is rejected by\n// default, which is safer than trying to enumerate every forbidden name. Keep in sync with the Mongoose\n// snapshot builder (build_portal_snapshot).\nfunction pc_rtm_snapshot_allowed_top_keys() {\n    return array( 'ok', 'version', 'status', 'generated_at', 'snapshot_at', 'full', 'flat_count',\n        'flats', 'demands', 'receipts', 'bank_details', 'bank_details_ok', 'caveat_7day', 'votes', 'member_flat_ids',\n        'shared_documents', 'planned_works', 'lease_wording', 'schedules', 'credit_notes' );\n}\n\nfunction pc_rtm_snapshot_unknown_top_key( $snap ) {\n    if ( ! is_array( $snap ) ) return '';\n    $allowed = pc_rtm_snapshot_allowed_top_keys();\n    foreach ( array_keys( $snap ) as $k ) {\n        if ( ! in_array( pc_rtm_normalize_key( $k ), $allowed, true ) ) return (string) $k;\n    }\n    return '';\n}\n\n// Defence in depth: reject a snapshot that carries any director-only / sensitive field ANYWHERE (nested\n// too), even though Mongoose already strips these before sending. Uses the normaliser so camelCase and\n// hyphenated synonyms are caught.\nfunction pc_rtm_snapshot_blocked_field( $obj ) {\n    $blocked = array( 'ftt_strategy', 'tribunal_strategy', 'witness_statement', 'witness_statements',\n        'skeleton_argument', 'without_prejudice', 'director_notes', 'director_commentary',\n        'supplier_payables', 'contractor_quotes', 'internal_evidence', 'evidence_bundle', 'case_theory',\n        'legal_advice', 'mongoose_audit', 'raw_bank_transaction', 'raw_claim_combination', 'claim_hash',\n        'hmac_pepper', 'claim_secret', 'pdf_storage_path' );\n    if ( is_array( $obj ) ) {\n        foreach ( $obj as $k => $v ) {\n            if ( in_array( pc_rtm_normalize_key( $k ), $blocked, true ) ) return (string) $k;\n            $found = pc_rtm_snapshot_blocked_field( $v );\n            if ( $found ) return $found;\n        }\n    }\n    return '';\n}\n\n// -- Snapshot validation into a staging object (H1) ---------------------------------\n// The snapshot is a source-of-truth MIRROR that gates access, balances, payment instructions and instant\n// provisioning, so a malformed-but-allowlisted payload must never replace the last-known-good. We validate\n// the incoming snapshot into a staging object against a versioned, structural schema BEFORE it is stored;\n// on ANY rejection the last-known-good (pc_rtm_snapshot) is left untouched and the rejection is logged\n// (pc_rtm_rejected_snapshot) so an operator can see what was refused and why.\n//\n// Deliberately NOT enforced here (documented for M): tightening every position/status ENUM to a fixed\n// vocabulary. Rejecting an unknown-but-valid enum value would freeze the portal on stale data (a worse\n// failure on a live financial surface), and the exact Mongoose vocabulary is not visible from this side.\n// The clear-cut corruption cases below (missing/duplicate identities, wrong types, unknown flat refs,\n// invalid emails, oversize, too-new schema, missing generation time) are enforced now.\n\n/** Structural validation. Returns '' when acceptable, else a short human reason. */\nfunction pc_rtm_validate_snapshot( $snap ) {\n    if ( ! is_array( $snap ) ) return 'snapshot is not an object';\n    // Schema version, when present, must be an integer this plugin understands (reject a newer major).\n    if ( array_key_exists( 'version', $snap ) ) {\n        $v = $snap['version'];\n        if ( ! is_int( $v ) && ! ( is_string( $v ) && ctype_digit( $v ) ) ) return 'version is not an integer';\n        $max = (int) apply_filters( 'pc_rtm_snapshot_schema_max', 99 );\n        if ( (int) $v > $max ) return 'snapshot schema version ' . (int) $v . ' is newer than this plugin supports';\n    }\n    // Flats: flat_id required + string + unique; payment_reference_display unique; email valid when present.\n    $flat_ids = array(); $refs = array();\n    foreach ( (array) ( $snap['flats'] ?? array() ) as $i => $f ) {\n        if ( ! is_array( $f ) ) return \"flat #$i is not an object\";\n        $fid = $f['flat_id'] ?? null;\n        if ( ! is_string( $fid ) || trim( $fid ) === '' ) return \"flat #$i has no valid flat_id\";\n        $k = strtoupper( str_replace( '-', '', $fid ) );\n        if ( isset( $flat_ids[ $k ] ) ) return 'duplicate flat_id ' . $fid;\n        $flat_ids[ $k ] = true;\n        $em = (string) ( $f['leaseholder_email'] ?? ( $f['email'] ?? '' ) );\n        if ( $em !== '' && ! is_email( $em ) ) return 'flat ' . $fid . ' has an invalid email';\n        $ref = strtolower( trim( (string) ( $f['payment_reference_display'] ?? '' ) ) );\n        if ( $ref !== '' ) {\n            if ( isset( $refs[ $ref ] ) ) return 'duplicate payment reference for flat ' . $fid;\n            $refs[ $ref ] = true;\n        }\n    }\n    // Demands: unique document_number + pdf_key; each demand's flat must exist in flats[].\n    $docs = array(); $pdf_keys = array();\n    foreach ( (array) ( $snap['demands'] ?? array() ) as $i => $d ) {\n        if ( ! is_array( $d ) ) return \"demand #$i is not an object\";\n        $dn = strtolower( trim( (string) ( $d['document_number'] ?? '' ) ) );\n        if ( $dn !== '' ) {\n            if ( isset( $docs[ $dn ] ) ) return 'duplicate document number ' . $dn;\n            $docs[ $dn ] = true;\n        }\n        $dfk = strtoupper( str_replace( '-', '', (string) ( $d['flat_id'] ?? '' ) ) );\n        if ( $dfk !== '' && ! isset( $flat_ids[ $dfk ] ) ) return 'demand ' . $dn . ' references unknown flat ' . (string) ( $d['flat_id'] ?? '' );\n        $pk = (string) ( $d['pdf_key'] ?? '' );\n        if ( $pk !== '' ) {\n            if ( isset( $pdf_keys[ $pk ] ) ) return 'duplicate pdf key ' . $pk;\n            $pdf_keys[ $pk ] = true;\n        }\n    }\n    // Receipts share the pdf_key namespace (a key identifies exactly one stored PDF).\n    foreach ( (array) ( $snap['receipts'] ?? array() ) as $i => $r ) {\n        if ( ! is_array( $r ) ) return \"receipt #$i is not an object\";\n        $pk = (string) ( $r['pdf_key'] ?? '' );\n        if ( $pk !== '' ) {\n            if ( isset( $pdf_keys[ $pk ] ) ) return 'duplicate pdf key ' . $pk;\n            $pdf_keys[ $pk ] = true;\n        }\n    }\n    return '';\n}\n\n/** Record a rejected snapshot (reason + its claimed generation time) WITHOUT touching last-known-good. */\nfunction pc_rtm_log_rejected_snapshot( $reason, $gen_raw ) {\n    update_option( 'pc_rtm_rejected_snapshot', array(\n        'at'           => current_time( 'mysql', true ),\n        'reason'       => (string) $reason,\n        'generated_at' => (string) $gen_raw,\n    ), false );\n    error_log( 'pc_rtm snapshot REJECTED (' . (string) $reason . ') , last-known-good preserved' );\n}\n\n/** Is the stored snapshot fresh enough to be trusted for instant provisioning / financial actions? (H2) */\nfunction pc_rtm_snapshot_is_fresh() {\n    $at = (string) get_option( 'pc_rtm_snapshot_at', '' );\n    if ( $at === '' ) return false;\n    $ts = strtotime( $at . ' UTC' );\n    if ( ! $ts ) return false;\n    $max_age = (int) apply_filters( 'pc_rtm_snapshot_max_age', 45 * DAY_IN_SECONDS );\n    return ( time() - $ts ) <= $max_age;\n}\n\n// -- Acknowledgement honesty (B6) ---------------------------------------------------\n// Mongoose PULLS a queue, applies each item on its own (the ledger) side, then calls one of the *-ack\n// routes so WordPress retires the pulled rows. The old callbacks cast a $wpdb->update()/query() result\n// (false on DB error, 0 on no-row) to an int and STILL returned ok:true, so a missing table or schema\n// drift left Mongoose believing WordPress had retired work it was in fact still serving. This helper\n// mirrors the Mongoose side's ack-on-success-only rule:\n//   - $wpdb->update() === false  -> a real DB error. Log it SAFELY (table + wpdb->last_error only, never\n//                                    any payload/secret) and abort the whole batch with HTTP 500 so\n//                                    Mongoose keeps the work and retries.\n//   - rows affected > 0          -> 'acked' (the pending row was retired now).\n//   - rows affected === 0        -> distinguish, per id: the row does not exist ('missing') vs it was\n//                                    already retired on an earlier ack ('already'). Both are idempotent-OK\n//                                    (re-acking is not an error), but a genuinely missing id is reported.\n// Never returns ok:true merely because the loop finished. Idempotency here is structural: the pending\n// predicate ($extra_pending, e.g. status='pending' / synced=0) means a second ack of the same id changes\n// zero rows and reports 'already', so no separate consumer op-id table is needed for acks.\nfunction pc_rtm_ack_id_list( $table, array $ids, array $set, array $extra_pending, $context ) {\n    global $wpdb;\n    $results = array();\n    $acked = 0;\n    $missing = 0;\n    foreach ( $ids as $raw ) {\n        $id = (int) $raw;\n        if ( $id <= 0 ) { $results[] = array( 'id' => $id, 'status' => 'missing' ); $missing++; continue; }\n        $updated = $wpdb->update( $table, $set, array_merge( array( 'id' => $id ), $extra_pending ) );\n        if ( $updated === false ) {\n            // Safe log: table name + MySQL error text only (no ids' payloads, no secrets).\n            error_log( sprintf( 'pc_rtm ack DB error [%s] table %s: %s', $context, $table, $wpdb->last_error ) );\n            return new WP_Error( 'ack_db_error',\n                'Acknowledgement failed because of a database error; the work was NOT retired and can be retried.',\n                array( 'status' => 500 ) );\n        }\n        if ( (int) $updated > 0 ) { $acked++; $results[] = array( 'id' => $id, 'status' => 'acked' ); continue; }\n        // Zero rows changed: was the id ever present at all? (Table name is code-controlled, not user input.)\n        $exists = (int) $wpdb->get_var( $wpdb->prepare( \"SELECT COUNT(*) FROM `$table` WHERE id = %d\", $id ) );\n        if ( $exists ) { $results[] = array( 'id' => $id, 'status' => 'already' ); }\n        else { $missing++; $results[] = array( 'id' => $id, 'status' => 'missing' ); }\n    }\n    return new WP_REST_Response( array(\n        'ok'      => true,       // honest: reached only when NO update returned false\n        'acked'   => $acked,     // rows retired by THIS call\n        'already' => count( $results ) - $acked - $missing,\n        'missing' => $missing,   // ids that do not exist (Mongoose should stop serving these)\n        'results' => $results,   // per-id: acked | already | missing\n    ), 200 );\n}\n\n// -- REST routes ------------------------------------------------------------------\nadd_action( 'rest_api_init', function () {\n    register_rest_route( '{{wordpress.namespace}}', '/sync/snapshot', array(\n        'methods' => 'POST', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function ( WP_REST_Request $req ) {\n            $ok = pc_rtm_verify_write( $req );\n            if ( $ok !== true ) return $ok;\n            // H1: bound the payload size before parsing (18 flats + metadata is tiny; PDFs are pushed\n            // separately, never inside the snapshot), so a runaway body cannot be ingested.\n            $raw = (string) $req->get_body();\n            $max_bytes = (int) apply_filters( 'pc_rtm_snapshot_max_bytes', 2 * 1024 * 1024 );\n            if ( strlen( $raw ) > $max_bytes ) {\n                pc_rtm_log_rejected_snapshot( 'snapshot exceeds ' . $max_bytes . ' bytes', '' );\n                return new WP_Error( 'snapshot_too_large', 'Snapshot payload is too large.', array( 'status' => 413 ) );\n            }\n            $snap = $req->get_json_params();\n            if ( ! is_array( $snap ) || empty( $snap['flats'] ) || ! is_array( $snap['flats'] ) ) {\n                pc_rtm_log_rejected_snapshot( 'missing or malformed flats', (string) ( is_array( $snap ) ? ( $snap['generated_at'] ?? '' ) : '' ) );\n                return new WP_Error( 'bad_snapshot', 'Snapshot missing or malformed flats.', array( 'status' => 400 ) );\n            }\n            // H1: a source-of-truth mirror MUST carry a parseable generation time. Without it, freshness and\n            // monotonic ordering cannot be established, so a missing/unparseable generated_at is REJECTED\n            // rather than being allowed to bypass the older-than-current comparison (audit H1).\n            $gen_raw = (string) ( $snap['generated_at'] ?? '' );\n            $incoming_gen = $gen_raw !== '' ? strtotime( $gen_raw ) : false;\n            if ( ! $incoming_gen ) {\n                pc_rtm_log_rejected_snapshot( 'missing or unparseable generated_at', $gen_raw );\n                return new WP_Error( 'bad_snapshot', 'Snapshot has no parseable generated_at; refusing to replace last-known-good.', array( 'status' => 400 ) );\n            }\n            // H1 monotonic revision: ignore an out-of-order OLDER-OR-EQUAL snapshot (by generation time), so a\n            // delayed retry or a same-timestamp rebuild can't restore/overwrite newer data.\n            $stored_rev = (int) get_option( 'pc_rtm_snapshot_revision', 0 );\n            if ( $stored_rev && $incoming_gen <= $stored_rev ) {\n                return new WP_REST_Response( array( 'ok' => true, 'ignored' => 'not_newer_than_current' ), 200 );\n            }\n            $unknown = pc_rtm_snapshot_unknown_top_key( $snap );\n            if ( $unknown ) {\n                pc_rtm_log_rejected_snapshot( 'unrecognised top-level section: ' . $unknown, $gen_raw );\n                return new WP_Error( 'unsafe_snapshot',\n                    'Snapshot contains an unrecognised top-level section not allowed in the leaseholder portal: ' . $unknown,\n                    array( 'status' => 400 ) );\n            }\n            $blocked = pc_rtm_snapshot_blocked_field( $snap );\n            if ( $blocked ) {\n                pc_rtm_log_rejected_snapshot( 'blocked field: ' . $blocked, $gen_raw );\n                return new WP_Error( 'unsafe_snapshot',\n                    'Snapshot contains a field not allowed in the leaseholder portal: ' . $blocked,\n                    array( 'status' => 400 ) );\n            }\n            // H1 structural validation into a staging object; last-known-good is untouched on rejection.\n            $invalid = pc_rtm_validate_snapshot( $snap );\n            if ( $invalid !== '' ) {\n                pc_rtm_log_rejected_snapshot( $invalid, $gen_raw );\n                return new WP_Error( 'invalid_snapshot',\n                    'Snapshot failed validation and was not applied (last-known-good kept): ' . $invalid,\n                    array( 'status' => 400 ) );\n            }\n            // Accepted: promote staging -> last-known-good and advance the monotonic revision.\n            update_option( 'pc_rtm_snapshot', wp_json_encode( $snap ), false );\n            update_option( 'pc_rtm_snapshot_at', gmdate( 'Y-m-d H:i:s', $incoming_gen ), false );\n            update_option( 'pc_rtm_snapshot_revision', (int) $incoming_gen, false );\n            // M7: reconcile the PDF store against what the new snapshot actually references, retiring\n            // orphaned/superseded PDFs past the retention window. Cheap for ~18 flats; never touches a\n            // still-referenced or recently-pushed PDF.\n            $reclaimed = pc_rtm_reconcile_pdfs( $snap );\n            return new WP_REST_Response( array( 'ok' => true, 'flats' => count( $snap['flats'] ), 'pdfs_reclaimed' => (int) $reclaimed ), 201 );\n        },\n    ) );\n    register_rest_route( '{{wordpress.namespace}}', '/sync/pending-claims', array(\n        'methods' => 'GET', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function () {\n            global $wpdb;\n            $rows = $wpdb->get_results( \"SELECT id, encrypted_payload_b64, created_at FROM {$wpdb->prefix}pc_rtm_claims WHERE status='pending' ORDER BY id ASC LIMIT 50\", ARRAY_A );\n            return array( 'claims' => $rows ? $rows : array() );\n        },\n    ) );\n    register_rest_route( '{{wordpress.namespace}}', '/sync/claim-decision/(?P<id>\\\\d+)', array(\n        'methods' => 'POST', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function ( WP_REST_Request $req ) {\n            $ok = pc_rtm_verify_write( $req );\n            if ( $ok !== true ) return $ok;\n            global $wpdb;\n            $id = (int) $req['id'];\n            $b = $req->get_json_params();\n            $decision = sanitize_text_field( $b['decision'] ?? '' );\n            $status = ( $decision === 'reject' ) ? 'rejected'\n                    : ( strpos( $decision, 'approve' ) === 0 ? 'approved_pending_email' : 'director_review' );\n            // B6: honest ack. false -> HTTP 500 (logged safely, nothing recorded); 0 rows -> the claim id\n            // does not exist (404) unless the SAME decision was already recorded (idempotent, ok).\n            $updated = $wpdb->update( $wpdb->prefix . 'pc_rtm_claims', array(\n                'status' => $status,\n                'decision' => $decision,\n                'flat_id' => sanitize_text_field( $b['flat_id'] ?? '' ),\n                'failure_message' => sanitize_textarea_field( $b['failure_message'] ?? '' ),\n                'decided_at' => current_time( 'mysql', true ),\n            ), array( 'id' => $id ) );\n            if ( $updated === false ) {\n                error_log( 'pc_rtm claim-decision DB error id=' . $id . ': ' . $wpdb->last_error );\n                return new WP_Error( 'ack_db_error', 'Claim decision failed (database error); no change recorded.', array( 'status' => 500 ) );\n            }\n            if ( (int) $updated === 0 ) {\n                $exists = (int) $wpdb->get_var( $wpdb->prepare( \"SELECT COUNT(*) FROM {$wpdb->prefix}pc_rtm_claims WHERE id = %d\", $id ) );\n                if ( ! $exists ) {\n                    return new WP_Error( 'claim_missing', 'No access request with that id exists.', array( 'status' => 404 ) );\n                }\n            }\n            return array( 'ok' => true, 'id' => $id, 'status' => $status, 'already_applied' => ( (int) $updated === 0 ) );\n        },\n    ) );\n    register_rest_route( '{{wordpress.namespace}}', '/sync/issues', array(\n        'methods' => 'GET', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function () {\n            global $wpdb;\n            $rows = $wpdb->get_results( \"SELECT id, flat_id, category, title, description, urgency, location, payment_date, amount_paid, reference_used, created_at AS submitted_at FROM {$wpdb->prefix}pc_rtm_issues WHERE synced=0 ORDER BY id ASC LIMIT 50\", ARRAY_A );\n            return array( 'issues' => $rows ? $rows : array() );\n        },\n    ) );\n    register_rest_route( '{{wordpress.namespace}}', '/sync/issue-synced/(?P<id>\\\\d+)', array(\n        'methods' => 'POST', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function ( WP_REST_Request $req ) {\n            $ok = pc_rtm_verify_write( $req );\n            if ( $ok !== true ) return $ok;\n            global $wpdb;\n            // B6: honest ack. A single id, but reuse the shared helper so false -> 500 and 0-rows is\n            // reported as already/missing rather than a blind ok:true.\n            return pc_rtm_ack_id_list( $wpdb->prefix . 'pc_rtm_issues', array( (int) $req['id'] ),\n                array( 'synced' => 1 ), array( 'synced' => 0 ), 'issue-synced' );\n        },\n    ) );\n    // Director financials mirror: Mongoose pushes the read-only summary; the admin Financials page\n    // renders it. Stored verbatim as an option; Mongoose remains the ledger and source of truth.\n    register_rest_route( '{{wordpress.namespace}}', '/sync/director-summary', array(\n        'methods' => 'POST', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function ( WP_REST_Request $req ) {\n            $ok = pc_rtm_verify_write( $req );\n            if ( $ok !== true ) return $ok;\n            $sum = $req->get_json_params();\n            if ( ! is_array( $sum ) || empty( $sum['ok'] ) ) {\n                return new WP_Error( 'bad_summary', 'Director summary malformed.', array( 'status' => 400 ) );\n            }\n            // Defence in depth: the financial mirror must not carry any director-only strategy field.\n            $blocked = pc_rtm_snapshot_blocked_field( $sum );\n            if ( $blocked ) {\n                return new WP_Error( 'unsafe_summary', 'Summary contains a blocked field: ' . $blocked, array( 'status' => 400 ) );\n            }\n            update_option( 'pc_rtm_director_summary', wp_json_encode( $sum ), false );\n            update_option( 'pc_rtm_director_summary_at', current_time( 'mysql', true ), false );\n            return new WP_REST_Response( array( 'ok' => true ), 201 );\n        },\n    ) );\n    // Member vote casts held here get pulled by Mongoose (behind CGNAT it cannot be pushed to).\n    register_rest_route( '{{wordpress.namespace}}', '/sync/vote-casts', array(\n        'methods' => 'GET', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function () {\n            global $wpdb;\n            $rows = $wpdb->get_results( \"SELECT id AS cast_id, vote_id, flat_id, choice FROM {$wpdb->prefix}pc_rtm_vote_casts WHERE synced=0 ORDER BY id ASC LIMIT 500\", ARRAY_A );\n            return array( 'casts' => $rows ? $rows : array() );\n        },\n    ) );\n    // Director-requested membership changes, pulled by Mongoose (which applies + confirms via snapshot).\n    register_rest_route( '{{wordpress.namespace}}', '/sync/member-changes', array(\n        'methods' => 'GET', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function () {\n            global $wpdb;\n            $rows = $wpdb->get_results( \"SELECT id AS change_id, flat_id, requested_member, requested_by, requested_at FROM {$wpdb->prefix}pc_rtm_membership_changes WHERE status='pending' ORDER BY id ASC LIMIT 500\", ARRAY_A );\n            return array( 'changes' => $rows ? $rows : array() );\n        },\n    ) );\n    register_rest_route( '{{wordpress.namespace}}', '/sync/member-changes-ack', array(\n        'methods' => 'POST', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function ( WP_REST_Request $req ) {\n            $ok = pc_rtm_verify_write( $req );\n            if ( $ok !== true ) return $ok;\n            $b = $req->get_json_params();\n            $ids = isset( $b['change_ids'] ) && is_array( $b['change_ids'] ) ? $b['change_ids'] : array();\n            global $wpdb;\n            return pc_rtm_ack_id_list( $wpdb->prefix . 'pc_rtm_membership_changes', $ids,\n                array( 'status' => 'applied', 'applied_at' => current_time( 'mysql', true ) ),\n                array( 'status' => 'pending' ), 'member-changes-ack' );\n        },\n    ) );\n    // Director \"mark paid\" queue: Mongoose (the ledger) pulls pending marks, records the payment, and acks.\n    register_rest_route( '{{wordpress.namespace}}', '/sync/payment-marks', array(\n        'methods' => 'GET', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function () {\n            global $wpdb;\n            $rows = $wpdb->get_results( \"SELECT id AS mark_id, flat_id, document_number, amount, received_date, requested_by, requested_at FROM {$wpdb->prefix}pc_rtm_payment_marks WHERE status='pending' ORDER BY id ASC LIMIT 500\", ARRAY_A );\n            return array( 'marks' => $rows ? $rows : array() );\n        },\n    ) );\n    register_rest_route( '{{wordpress.namespace}}', '/sync/payment-marks-ack', array(\n        'methods' => 'POST', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function ( WP_REST_Request $req ) {\n            $ok = pc_rtm_verify_write( $req );\n            if ( $ok !== true ) return $ok;\n            $b = $req->get_json_params();\n            $ids = isset( $b['mark_ids'] ) && is_array( $b['mark_ids'] ) ? $b['mark_ids'] : array();\n            global $wpdb;\n            return pc_rtm_ack_id_list( $wpdb->prefix . 'pc_rtm_payment_marks', $ids,\n                array( 'status' => 'applied', 'applied_at' => current_time( 'mysql', true ) ),\n                array( 'status' => 'pending' ), 'payment-marks-ack' );\n        },\n    ) );\n    // Director \"generate missing document\" requests: Mongoose pulls the pending queue, builds each and\n    // pushes it back on the next snapshot, then acks so it is not re-served.\n    register_rest_route( '{{wordpress.namespace}}', '/sync/doc-requests', array(\n        'methods' => 'GET', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function () {\n            global $wpdb;\n            $rows = $wpdb->get_results( \"SELECT id AS request_id, flat_id, document_number, doc_type, requested_by, requested_at FROM {$wpdb->prefix}pc_rtm_doc_requests WHERE status='pending' ORDER BY id ASC LIMIT 500\", ARRAY_A );\n            return array( 'requests' => $rows ? $rows : array() );\n        },\n    ) );\n    register_rest_route( '{{wordpress.namespace}}', '/sync/doc-requests-ack', array(\n        'methods' => 'POST', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function ( WP_REST_Request $req ) {\n            $ok = pc_rtm_verify_write( $req );\n            if ( $ok !== true ) return $ok;\n            $b = $req->get_json_params();\n            $ids = isset( $b['request_ids'] ) && is_array( $b['request_ids'] ) ? $b['request_ids'] : array();\n            global $wpdb;\n            return pc_rtm_ack_id_list( $wpdb->prefix . 'pc_rtm_doc_requests', $ids,\n                array( 'status' => 'applied', 'applied_at' => current_time( 'mysql', true ) ),\n                array( 'status' => 'pending' ), 'doc-requests-ack' );\n        },\n    ) );\n    // Per-flat linked WordPress accounts + last-login, pulled by Mongoose to enrich the Cockpit\n    // Flats & Members view (who has onboarded, when they last signed in). Read-only, admin-gated.\n    register_rest_route( '{{wordpress.namespace}}', '/sync/flat-accounts', array(\n        'methods' => 'GET', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function () {\n            $map = function_exists( 'pc_rtm_flat_accounts_map' ) ? pc_rtm_flat_accounts_map() : array();\n            return array( 'accounts' => $map );\n        },\n    ) );\n    register_rest_route( '{{wordpress.namespace}}', '/sync/vote-casts-synced', array(\n        'methods' => 'POST', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function ( WP_REST_Request $req ) {\n            $ok = pc_rtm_verify_write( $req );\n            if ( $ok !== true ) return $ok;\n            $b = $req->get_json_params();\n            $ids = isset( $b['cast_ids'] ) && is_array( $b['cast_ids'] ) ? $b['cast_ids'] : array();\n            global $wpdb;\n            return pc_rtm_ack_id_list( $wpdb->prefix . 'pc_rtm_vote_casts', $ids,\n                array( 'synced' => 1 ), array( 'synced' => 0 ), 'vote-casts-synced' );\n        },\n    ) );\n} );\n\n// -- Public claim form (sealed-box; ciphertext only ever stored) --------------------\nadd_shortcode( 'pc_rtm_claim_form', 'pc_rtm_claim_form_shortcode' );\nadd_shortcode( 'rtm_signup', 'pc_rtm_claim_form_shortcode' );\nfunction pc_rtm_claim_form_shortcode() {\n    $portal_url = function_exists( 'pc_rtm_portal_page_url' ) ? pc_rtm_portal_page_url() : home_url();\n    $user = wp_get_current_user();\n    $my_flat = ( $user && $user->exists() && function_exists( 'pc_rtm_portal_user_flat_id' ) ) ? pc_rtm_portal_user_flat_id() : '';\n    if ( $my_flat !== '' ) {\n        // Only say \"ready\" if this login is STILL the current on-file leaseholder. A sold-on owner keeps a\n        // stored flat id but is no longer entitled; telling them the portal is ready and then denying them\n        // one click later is exactly the whiplash the audit flagged. Fall through to a proper explanation.\n        $authorized = ! function_exists( 'pc_rtm_email_authorized_for_flat' )\n            || pc_rtm_email_authorized_for_flat( $user->user_email, $my_flat );\n        if ( $authorized ) {\n            return pc_rtm_public_shell( 'signup', 'Already verified', 'Your leaseholder portal is ready', 'You are signed in and linked to a flat.', '<div class=\"pc-rtm-card\"><p><a class=\"pc-rtm-button\" href=\"' . esc_url( $portal_url ) . '\">Open my portal</a></p></div>' );\n        }\n        if ( function_exists( 'pc_rtm_access_problem_shell' ) && function_exists( 'pc_rtm_flat_access_state' ) ) {\n            return pc_rtm_access_problem_shell( pc_rtm_flat_access_state( $user->user_email, $my_flat ) );\n        }\n    }\n    if ( ! function_exists( 'sodium_crypto_box_seal' ) ) {\n        return pc_rtm_public_shell( 'signup', 'Temporarily unavailable', 'Leaseholder sign up', 'The site cannot accept access requests until the server encryption library is available.', '<div class=\"pc-rtm-card\"><p>Please contact the RTM directors if you need access urgently.</p></div>' );\n    }\n    $pub_b64 = (string) get_option( 'pc_rtm_claim_public_key', '' );\n    if ( $pub_b64 === '' ) {\n        return pc_rtm_public_shell( 'signup', 'Opening soon', 'Leaseholder sign up', 'The portal is installed, but access requests are not open yet.', '<div class=\"pc-rtm-card\"><p>The RTM company is still finishing setup. Please check back shortly.</p></div>' );\n    }\n    $pub = base64_decode( $pub_b64, true );\n    if ( $pub === false || strlen( $pub ) !== SODIUM_CRYPTO_BOX_PUBLICKEYBYTES ) {\n        return pc_rtm_public_shell( 'signup', 'Configuration needed', 'Leaseholder sign up', 'The public verification key is not valid yet.', '<div class=\"pc-rtm-card\"><p>Please contact the RTM company so they can correct the portal settings.</p></div>' );\n    }\n\n    $notice = '';\n    $email_value = $user && $user->exists() ? $user->user_email : '';\n    if ( isset( $_POST['pc_rtm_claim_nonce'] ) ) {\n        $nonce = sanitize_text_field( wp_unslash( $_POST['pc_rtm_claim_nonce'] ?? '' ) );\n        if ( ! wp_verify_nonce( $nonce, 'pc_rtm_claim' ) ) {\n            $notice = pc_rtm_notice_html( 'Your session expired. Please reload the page and try again.', 'error' );\n        } elseif ( trim( (string) wp_unslash( $_POST['pc_rtm_hp_check'] ?? '' ) ) !== '' ) {\n            // Honeypot: return a neutral success message, but store nothing.\n            $notice = pc_rtm_notice_html( 'Thank you. Your access request has been submitted and is being checked.', 'success' );\n        } else {\n            $payment_reference = sanitize_text_field( wp_unslash( $_POST['payment_reference'] ?? '' ) );\n            $document_number   = sanitize_text_field( wp_unslash( $_POST['document_number'] ?? '' ) );\n            $email             = sanitize_email( wp_unslash( $_POST['claim_email'] ?? '' ) );\n            $email_value       = $email;\n            // We do NOT ask for or trust a typed name: it is not used for verification, so a stranger\n            // could enter any name (\"Luke Skywalker\"). The real leaseholder name comes from the RTM\n            // records once Mongoose matches the reference to a flat, and that is what the account uses.\n            if ( $payment_reference === '' || $document_number === '' || ! is_email( $email ) ) {\n                // Field errors do NOT count against the throttle, so a genuine user correcting a typo is\n                // never locked out; only actually-stored claims are rate-limited (see below).\n                $notice = pc_rtm_notice_html( 'Please enter your email, payment reference and document number from your service charge demand.', 'error' );\n            } elseif ( pc_rtm_claim_rate_limited() ) {\n                // Throttle STORED claims per IP so a scripted run cannot flood the table / director emails.\n                $notice = pc_rtm_notice_html( 'Thank you. If your access request has not already been received, please try again a little later.', 'success' );\n            } else {\n                // WordPress-side INSTANT match against the snapshot Mongoose already pushed: the payment\n                // reference + document number + on-file email must ALL agree, and then the account is\n                // created right away with no async wait (the strict rule the owner chose).\n                // One neutral message for BOTH the instant-match and the queued paths, so the page never\n                // reveals whether the details matched (no email-enumeration oracle). It is encouraging\n                // enough that a matched leaseholder (whose email is already arriving) does not think\n                // nothing will happen for days, without promising anything to a non-match.\n                $neutral_msg = 'Thank you. Your request has been received securely. If we could confirm your details automatically, a set-password email should arrive shortly, so please check your inbox (and spam or junk folder). Otherwise we will email you after a quick manual check, usually within a couple of working days. Please do not submit again in the meantime.';\n                $matched_flat = pc_rtm_signup_match_flat( $payment_reference, $document_number, $email );\n                if ( $matched_flat !== '' && function_exists( 'pc_rtm_do_provision' ) ) {\n                    $prov = pc_rtm_do_provision( $email, $matched_flat, pc_rtm_signup_flat_name( $matched_flat ) );\n                    if ( ! is_wp_error( $prov ) ) {\n                        pc_rtm_claim_record_stored(); // a real account was created: this one counts against the throttle\n                        // Recovery for a genuine dead end: the account exists but the set-password email was\n                        // NOT accepted by WordPress. Without this, the leaseholder waits for a mail that\n                        // never comes, with no queue and nobody alerted. No mail-queue machinery for 18\n                        // flats: just alert the RTM mailbox so a human resends the link. The page still\n                        // shows the same neutral message (no oracle).\n                        if ( is_array( $prov ) && empty( $prov['email_sent'] ) && function_exists( 'pc_rtm_notify_director' ) ) {\n                            pc_rtm_notify_director( 'Leaseholder portal account created but onboarding email failed',\n                                array( 'A leaseholder account was created for flat ' . $matched_flat . ', but the set-password / welcome email could not be sent.',\n                                       'Please send them a password-reset link from Users in WordPress so they can finish signing in.' ) );\n                        }\n                        $email_value = '';\n                        $notice = pc_rtm_notice_html( $neutral_msg, 'success' );\n                    } else {\n                        $matched_flat = ''; // unexpected provisioning failure: fall back to the queue so nothing is lost\n                    }\n                }\n                if ( $matched_flat === '' ) {\n                    // Not a full match (wrong details, OR a correct reference+document but a different/no\n                    // on-file email): seal + queue for Mongoose / the director to handle. The message is\n                    // NEUTRAL and identical to a no-match, so this is never an oracle for which references\n                    // are valid. Mongoose emails the outcome (approved, or \"could not confirm, contact us\").\n                    $payload = wp_json_encode( array(\n                        'payment_reference' => $payment_reference,\n                        'document_number'   => $document_number,\n                        'email'             => $email,\n                        'submitted_at'      => current_time( 'mysql', true ),\n                        'site_url'          => home_url(),\n                    ) );\n                    try {\n                        $sealed = base64_encode( sodium_crypto_box_seal( $payload, $pub ) );\n                    } catch ( Throwable $e ) {\n                        $sealed = '';\n                    }\n                    if ( $sealed === '' ) {\n                        $notice = pc_rtm_notice_html( 'Sorry, we could not encrypt your access request just now. Please try again shortly.', 'error' );\n                    } else {\n                        global $wpdb;\n                        $ins = $wpdb->insert( $wpdb->prefix . 'pc_rtm_claims', array(\n                            'encrypted_payload_b64' => $sealed,\n                            'status' => 'pending',\n                            'created_at' => current_time( 'mysql', true ),\n                        ) );\n                        $notice = ( $ins === false )\n                            ? pc_rtm_notice_html( 'Sorry, we could not record your access request just now. Please try again shortly.', 'error' )\n                            : pc_rtm_notice_html( $neutral_msg, 'success' );\n                        if ( $ins !== false ) {\n                            pc_rtm_claim_record_stored(); // a claim row was genuinely stored: count it\n                            $email_value = '';\n                            if ( function_exists( 'pc_rtm_notify_director' ) ) {\n                                pc_rtm_notify_director( 'New leaseholder access request',\n                                    array( 'A leaseholder has submitted an access request through the sign-up page.',\n                                           'The RTM company will verify it and follow up.' ) );\n                            }\n                        }\n                    }\n                }\n            }\n        }\n    }\n\n    $login = wp_login_form( array(\n        'echo' => false,\n        'redirect' => $portal_url ?: home_url(),\n        'form_id' => 'pc_rtm_loginform',\n        'label_username' => __( 'Email address', '{{wordpress.plugin_slug}}' ),\n        'label_log_in' => __( 'Sign in to my portal', '{{wordpress.plugin_slug}}' ),\n        'required_username' => true,\n        'required_password' => true,\n    ) );\n\n    $form  = $notice;\n    $form .= '<div class=\"pc-rtm-auth-grid\">';\n    // Request access as a short, one-question-at-a-time workflow. The old form put the payment reference\n    // first and exposed every field at once; best practice is to lead with a gentle \"are you a\n    // leaseholder?\" gate, then ask one thing per step with Next. Progressive enhancement: with no\n    // JavaScript every step is simply visible and the single submit still works (never a dead end).\n    $form .= '<div class=\"pc-rtm-card\"><h3>Are you a {{rtm.name}} leaseholder?</h3>';\n    $form .= '<form method=\"post\" class=\"pc-rtm-form pc-rtm-wizard\" id=\"pc-rtm-wizard\">' . wp_nonce_field( 'pc_rtm_claim', 'pc_rtm_claim_nonce', true, false );\n    // Honeypot: a non-semantic field name and aria-hidden, so password managers / browser autofill do\n    // not fill it and falsely flag a genuine leaseholder as a bot. Bots that fill everything still trip it.\n    $form .= '<div class=\"pc-rtm-honeypot\" aria-hidden=\"true\"><label>Leave this field blank <input type=\"text\" name=\"pc_rtm_hp_check\" tabindex=\"-1\" autocomplete=\"off\" value=\"\"></label></div>';\n    // M4: programmatic step progress. portal.js fills this with \"Step X of Y\" and it is announced via\n    // aria-live as the leaseholder moves through the wizard. Hidden by CSS when JavaScript is off.\n    $form .= '<p class=\"pc-rtm-wizard-progress\" role=\"status\" aria-live=\"polite\"></p>';\n    // Step 0: gentle intro / gate\n    $contact_url = function_exists( 'pc_rtm_page_url' ) ? pc_rtm_page_url( 'pc_rtm_contact_page_id' ) : '';\n    $help = $contact_url\n        ? ' <a href=\"' . esc_url( $contact_url ) . '\">Cannot find your demand?</a>'\n        : '';\n    $form .= '<div class=\"pc-rtm-step\" data-step=\"0\">'\n        . '<p>Request secure access to your online portal, where you can see your service charge demands, payment details and documents.</p>'\n        . '<p class=\"pc-rtm-muted\">It takes about a minute. Please have your latest service charge demand to hand.' . $help . '</p>'\n        . '<p class=\"pc-rtm-wizard-nav\"><button type=\"button\" class=\"pc-rtm-next\">Start</button></p></div>';\n    // Step 1: email. We deliberately do NOT ask for a name: it is not used to verify anything, so a\n    // stranger could type any name. Your real name is taken from the RTM records once your reference is\n    // matched to a flat, and that is what your portal account uses.\n    $form .= '<div class=\"pc-rtm-step\" data-step=\"1\"><label for=\"pc_rtm_claim_email\">Your email address</label>'\n        . '<p class=\"pc-rtm-muted\">We use this to set up your account and to reply to you.</p>'\n        . '<input id=\"pc_rtm_claim_email\" type=\"email\" name=\"claim_email\" autocomplete=\"email\" value=\"' . esc_attr( $email_value ) . '\" required>'\n        . '<p class=\"pc-rtm-wizard-nav\"><button type=\"button\" class=\"pc-rtm-back\">Back</button> <button type=\"button\" class=\"pc-rtm-next\">Next</button></p></div>';\n    // Step 3: payment reference. Label + hint match exactly what the demand prints (\"using payment\n    // reference ...\"), so the leaseholder knows precisely which value to copy.\n    $form .= '<div class=\"pc-rtm-step\" data-step=\"3\"><label for=\"pc_rtm_payment_reference\">Payment reference</label>'\n        . '<p class=\"pc-rtm-muted\">On your service charge demand, in the \"How to pay\" section (\"using payment reference ...\"). Case does not matter. It is encrypted the moment you submit it and is never shown to anyone on the website.</p>'\n        . '<input id=\"pc_rtm_payment_reference\" type=\"text\" name=\"payment_reference\" autocomplete=\"off\" required>'\n        . '<p class=\"pc-rtm-wizard-nav\"><button type=\"button\" class=\"pc-rtm-back\">Back</button> <button type=\"button\" class=\"pc-rtm-next\">Next</button></p></div>';\n    // Step 4: document number + submit. \"Document number\" is the exact label printed on the demand.\n    $form .= '<div class=\"pc-rtm-step\" data-step=\"4\"><label for=\"pc_rtm_document_number\">Document number</label>'\n        . '<p class=\"pc-rtm-muted\">Printed near the top of your service charge demand, next to the date, labelled \"Document number\". Case does not matter.</p>'\n        . '<input id=\"pc_rtm_document_number\" type=\"text\" name=\"document_number\" autocomplete=\"off\" required>'\n        . '<p class=\"pc-rtm-wizard-nav\"><button type=\"button\" class=\"pc-rtm-back\">Back</button> <button type=\"submit\">Request access</button></p></div>';\n    $form .= '</form></div>';\n    $form .= '<div class=\"pc-rtm-card\"><h3>Already approved?</h3>' . $login . '<p class=\"pc-rtm-muted\"><a href=\"' . esc_url( wp_lostpassword_url( $portal_url ) ) . '\">Forgotten your password?</a></p></div>';\n    $form .= '</div>';\n    $form .= '<div class=\"pc-rtm-card\"><h3>What happens next?</h3><ol class=\"pc-rtm-steps\"><li><div><strong>We check your details against our leaseholder records.</strong><br><span class=\"pc-rtm-muted\">Your payment and demand references are encrypted the moment you submit them and are never shown to anyone on the website.</span></div></li><li><div><strong>We set up your secure portal account.</strong><br><span class=\"pc-rtm-muted\">Once approved, you receive an email with a link to choose your password.</span></div></li><li><div><strong>You sign in to your leaseholder portal.</strong><br><span class=\"pc-rtm-muted\">You will see only your flat: your demands, payment details and documents.</span></div></li></ol></div>';\n    return pc_rtm_public_shell( 'signup', 'Leaseholder access', 'Sign in or request access', 'Your private {{rtm.name}} leaseholder portal for service charge documents, payment details and documents.', $form );\n}\n\n/** Styles + behaviour for the step-by-step access-request wizard. Progressive enhancement only: the\n *  steps are ordinary form fields, so with JavaScript disabled they all show and the single submit\n *  still works. With JavaScript, one step shows at a time and Next validates that step first. */\n// The wizard stepper CSS + JS now live in the ENQUEUED assets/portal.css and assets/portal.js, because\n// an inline <script>/<style> in shortcode content was being dropped (theme/CSP), which left every step\n// visible. Kept as a no-op so any stray caller is harmless.\nfunction pc_rtm_wizard_assets() {\n    return '';\n}\n\n// -- Portal admin page (public key + sync status) ------------------------------------\nadd_action( 'admin_menu', function () {\n    add_submenu_page( '{{wordpress.plugin_slug}}', 'Leaseholder Portal', 'Portal',\n        'rtm_manage_portal', 'pc-rtm-portal', 'pc_rtm_portal_page' );\n}, 20 );\nfunction pc_rtm_portal_page() {\n    // C3: the encryption key is a CONNECTION SECRET. Only a full site administrator may change it, never\n    // an RTM Director (rtm_manage_portal). A malicious/compromised director must not be able to swap in a\n    // key they control and decrypt every future access request, upload and auditor note.\n    $can_edit_key = current_user_can( 'manage_options' );\n    if ( isset( $_POST['pc_rtm_portal_save'] ) && check_admin_referer( 'pc_rtm_portal' ) ) {\n        if ( ! $can_edit_key ) {\n            wp_die( esc_html__( 'Only a site administrator can change the encryption key.', '{{wordpress.plugin_slug}}' ) );\n        }\n        update_option( 'pc_rtm_claim_public_key', sanitize_text_field( wp_unslash( $_POST['claim_public_key'] ?? '' ) ) );\n        echo '<div class=\"notice notice-success\"><p>Portal settings saved.</p></div>';\n    }\n    global $wpdb;\n    $pub = get_option( 'pc_rtm_claim_public_key', '' );\n    $snap_at = get_option( 'pc_rtm_snapshot_at', '' );\n    $snap = json_decode( (string) get_option( 'pc_rtm_snapshot', '' ), true );\n    $snap_flats = ( is_array( $snap ) && is_array( $snap['flats'] ?? null ) ) ? $snap['flats'] : array();\n    $pending = (int) $wpdb->get_var( \"SELECT COUNT(*) FROM {$wpdb->prefix}pc_rtm_claims WHERE status='pending'\" );\n    $unsynced = (int) $wpdb->get_var( \"SELECT COUNT(*) FROM {$wpdb->prefix}pc_rtm_issues WHERE synced=0\" );\n    ?>\n    <div class=\"wrap\"><h1>Leaseholder Portal</h1>\n    <p>Snapshot: <?php echo $snap_at ? esc_html( $snap_at . ' UTC, ' . count( $snap_flats ) . ' flats' ) : 'never pushed'; ?>\n       &middot; Pending access requests: <?php echo esc_html( $pending ); ?>\n       &middot; Issues awaiting Mongoose: <?php echo esc_html( $unsynced ); ?></p>\n    <?php if ( $can_edit_key ) : ?>\n    <form method=\"post\">\n    <?php wp_nonce_field( 'pc_rtm_portal' ); ?>\n    <table class=\"form-table\">\n      <tr><th>Access-request public key</th>\n        <td><input name=\"claim_public_key\" value=\"<?php echo esc_attr( $pub ); ?>\" class=\"regular-text\" placeholder=\"(base64 from: mongoose rtm portal public-key)\" />\n        <p class=\"description\">Mongoose's PUBLIC access-request key. The private key never leaves the Mac. The webhook secret on the Settings page also verifies snapshot pushes.</p></td></tr>\n    </table>\n    <p><input type=\"submit\" name=\"pc_rtm_portal_save\" class=\"button-primary\" value=\"Save\" /></p>\n    </form>\n    <?php else : ?>\n    <p class=\"description\"><?php echo esc_html__( 'The encryption key is a connection secret and can only be changed by a site administrator.', '{{wordpress.plugin_slug}}' ); ?> <?php echo $pub !== '' ? esc_html__( 'A key is currently configured.', '{{wordpress.plugin_slug}}' ) : esc_html__( 'No key is configured yet , ask an administrator to set it.', '{{wordpress.plugin_slug}}' ); ?></p>\n    <?php endif; ?>\n    <p>Add the claim form to any page with the shortcode <code>[pc_rtm_claim_form]</code>.</p>\n\n    <h2>Access requests</h2>\n    <?php if ( function_exists( 'pc_rtm_render_requests_table' ) ) { pc_rtm_render_requests_table(); } ?>\n    <p class=\"description\">Sort by any column. Access-request contents stay encrypted on WordPress; only status metadata is shown here. Mongoose decrypts and decides on the Mac.</p>\n\n    <h2>Recent leaseholder requests</h2>\n    <?php $reqs = $wpdb->get_results( \"SELECT id, flat_id, category, title, synced, created_at FROM {$wpdb->prefix}pc_rtm_issues ORDER BY id DESC LIMIT 25\", ARRAY_A ); ?>\n    <table class=\"widefat striped\"><thead><tr><th>ID</th><th>Flat</th><th>Category</th><th>Subject</th><th>Pulled by Mongoose</th><th>Submitted (UTC)</th></tr></thead><tbody>\n    <?php if ( $reqs ) : foreach ( $reqs as $r ) : ?>\n      <tr><td><?php echo esc_html( $r['id'] ); ?></td><td><?php echo esc_html( $r['flat_id'] ); ?></td>\n      <td><?php echo esc_html( $r['category'] ); ?></td><td><?php echo esc_html( $r['title'] ); ?></td>\n      <td><?php echo $r['synced'] ? 'yes' : 'pending'; ?></td><td><?php echo esc_html( $r['created_at'] ); ?></td></tr>\n    <?php endforeach; else : ?>\n      <tr><td colspan=\"6\">No requests submitted yet.</td></tr>\n    <?php endif; ?>\n    </tbody></table>\n    </div>\n    <?php\n}\n\n// \u2500\u2500 Demand PDFs + diagnostics (reconciled from the Pro build, adapted to the rtm_flat_id model) \u2500\u2500\u2500\u2500\u2500\u2500\nadd_action( 'rest_api_init', function () {\n    register_rest_route( '{{wordpress.namespace}}', '/health', array(\n        'methods' => 'GET', 'permission_callback' => '__return_true',\n        'callback' => function () { return array( 'ok' => true, 'plugin' => '{{wordpress.plugin_slug}}', 'version' => PC_RTM_VERSION ); },\n    ) );\n    // Signed self-description so Mongoose (and, through Mongoose's MCP connector, Claude.ai) can discover\n    // the LIVE capability + data surface instead of trusting a hardcoded copy. HMAC gated (internal\n    // structure, not public); the route list and table list are derived from the live registry and the\n    // single manifest, so they stay accurate as the plugin evolves. Returns NO secrets.\n    register_rest_route( '{{wordpress.namespace}}', '/capabilities', array(\n        'methods' => 'GET', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => 'pc_rtm_rest_capabilities',\n    ) );\n    register_rest_route( '{{wordpress.namespace}}', '/sync/snapshot-status', array(\n        'methods' => 'GET', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => function () {\n            $snap = json_decode( (string) get_option( 'pc_rtm_snapshot', '' ), true );\n            global $wpdb;\n            return array(\n                'ok' => true, 'version' => PC_RTM_VERSION,\n                'snapshot_at' => get_option( 'pc_rtm_snapshot_at', '' ),\n                'flat_count'  => is_array( $snap ) ? count( $snap['flats'] ?? array() ) : 0,\n                'pdf_count'   => (int) $wpdb->get_var( \"SELECT COUNT(*) FROM {$wpdb->prefix}pc_rtm_pdfs\" ),\n            );\n        },\n    ) );\n    // Mongoose pushes a demand PDF (HMAC + admin gated). Sensitive strategy PDFs never come here.\n    register_rest_route( '{{wordpress.namespace}}', '/sync/demand-pdf', array(\n        'methods' => 'POST', 'permission_callback' => 'pc_rtm_admin_permission',\n        'callback' => 'pc_rtm_receive_demand_pdf',\n    ) );\n    // The leaseholder downloads their OWN flat's demand PDF (login + flat-match gated).\n    register_rest_route( '{{wordpress.namespace}}', '/portal/demand-pdf/(?P<key>[A-Za-z0-9_\\-]+)', array(\n        'methods' => 'GET', 'permission_callback' => 'pc_rtm_can_download_demand_pdf',\n        'callback' => 'pc_rtm_serve_demand_pdf',\n    ) );\n} );\n\n/**\n * Machine-readable self-description of the plugin (HMAC gated, admin-scoped). Lets Mongoose, and through\n * Mongoose's MCP connector Claude.ai, discover the plugin's full capability and data surface LIVE rather\n * than relying on a hardcoded, drift-prone copy. Two sections are derived so they can never go stale:\n *   - rest_routes: read from the WordPress route registry (rest_get_server) and filtered to this plugin's\n *     namespace, so it always mirrors what is actually registered;\n *   - tables: taken from the single owned-tables manifest (pc_rtm_owned_tables), the one authoritative list.\n * Returns NO secrets: no keys, tokens or bank details, only names and shapes.\n */\nfunction pc_rtm_rest_capabilities( WP_REST_Request $req ) {\n    $ok = pc_rtm_verify_write( $req );\n    if ( $ok !== true ) return $ok;\n\n    // rest_routes: derived LIVE from the route registry, filtered to the {{wordpress.namespace}} namespace, so the\n    // list can never drift from what the plugin actually registers. The bare namespace index is skipped.\n    $rest_routes = array();\n    if ( function_exists( 'rest_get_server' ) ) {\n        foreach ( rest_get_server()->get_routes() as $route => $handlers ) {\n            if ( strpos( $route, '/{{wordpress.namespace}}/' ) !== 0 ) continue;\n            $methods = array();\n            foreach ( (array) $handlers as $handler ) {\n                if ( empty( $handler['methods'] ) || ! is_array( $handler['methods'] ) ) continue;\n                foreach ( $handler['methods'] as $method => $enabled ) {\n                    if ( $enabled ) $methods[ $method ] = true;\n                }\n            }\n            $rest_routes[] = array( 'route' => $route, 'methods' => array_keys( $methods ) );\n        }\n        usort( $rest_routes, function ( $a, $b ) { return strcmp( $a['route'], $b['route'] ); } );\n    }\n\n    // tables: from the single manifest helper, so a new table shows up here automatically.\n    $tables = function_exists( 'pc_rtm_owned_tables' ) ? array_values( pc_rtm_owned_tables() ) : array();\n\n    // admin_pages: the submenu slugs/titles the plugin registers under the RTM menu (assembled in code, no\n    // single registry exists; Access requests is a section of the Portal page).\n    $admin_pages = array(\n        array( 'slug' => '{{wordpress.plugin_slug}}', 'title' => __( '{{rtm.name}} RTM', '{{wordpress.plugin_slug}}' ) ),\n        array( 'slug' => 'pc-rtm-charges',    'title' => __( 'Service Charges', '{{wordpress.plugin_slug}}' ) ),\n        array( 'slug' => 'pc-rtm-members',    'title' => __( 'Flats & Members', '{{wordpress.plugin_slug}}' ) ),\n        array( 'slug' => 'pc-rtm-comms',      'title' => __( 'Communications', '{{wordpress.plugin_slug}}' ) ),\n        array( 'slug' => 'pc-rtm-portal',     'title' => __( 'Leaseholder Portal', '{{wordpress.plugin_slug}}' ) ),\n        array( 'slug' => 'pc-rtm-documents',  'title' => __( 'Document Intake', '{{wordpress.plugin_slug}}' ) ),\n        array( 'slug' => 'pc-rtm-send-log',   'title' => __( 'Sent mail log', '{{wordpress.plugin_slug}}' ) ),\n        array( 'slug' => 'pc-rtm-settings',   'title' => __( 'Settings', '{{wordpress.plugin_slug}}' ) ),\n    );\n\n    // queues: the command queues Mongoose drains, each backed by a real table and a sync route pair.\n    $queues = array( 'doc_requests', 'payment_marks', 'member_changes', 'vote_casts' );\n\n    // capabilities: short human-readable statements of what the plugin can actually do.\n    $capabilities = array(\n        'send demand email with PDF',\n        'send receipt email',\n        'record payment mark queue',\n        'director RTM-membership change queue',\n        'director document-request queue',\n        'member vote casts',\n        'director financials mirror',\n        'sent-mail audit log',\n        'leaseholder portal',\n        'access-request intake and provisioning',\n        'document intake with auditor notes',\n        'demand/receipt PDF store',\n    );\n\n    return new WP_REST_Response( array(\n        'ok'           => true,\n        'plugin'       => '{{wordpress.plugin_slug}}',\n        'version'      => PC_RTM_VERSION,\n        'db_version'   => (string) get_option( 'pc_rtm_portal_db_version', '' ),\n        'rest_routes'  => $rest_routes,\n        'tables'       => $tables,\n        'admin_pages'  => $admin_pages,\n        'queues'       => $queues,\n        'capabilities' => $capabilities,\n    ), 200 );\n}\n\n/** The current user's flat id, from the same meta the portal binds on provisioning. */\nfunction pc_rtm_portal_user_flat_id() {\n    $u = wp_get_current_user();\n    if ( ! $u || ! $u->exists() ) return '';\n    return (string) get_user_meta( $u->ID, 'rtm_flat_id', true );\n}\n\nfunction pc_rtm_receive_demand_pdf( WP_REST_Request $req ) {\n    $ok = pc_rtm_verify_write( $req );\n    if ( $ok !== true ) return $ok;\n    $b       = $req->get_json_params();\n    $b       = is_array( $b ) ? $b : array();\n    $pdf_key = sanitize_text_field( (string) ( $b['pdf_key'] ?? '' ) );\n    $doc     = sanitize_text_field( (string) ( $b['document_number'] ?? '' ) );\n    $flat_id = strtoupper( preg_replace( '/[^A-Za-z0-9\\-]/', '', (string) ( $b['flat_id'] ?? '' ) ) );\n    $content = (string) ( $b['content_b64'] ?? '' );\n    $sha     = sanitize_text_field( (string) ( $b['sha256'] ?? '' ) );\n    if ( $pdf_key === '' || $doc === '' || $flat_id === '' || $content === '' ) {\n        return new WP_Error( 'bad_pdf', 'pdf_key, document_number, flat_id and content_b64 are required', array( 'status' => 400 ) );\n    }\n    $raw = base64_decode( $content, true );\n    if ( ! is_string( $raw ) || substr( $raw, 0, 4 ) !== '%PDF' ) {\n        return new WP_Error( 'bad_pdf_content', 'content_b64 is not valid base64 PDF data.', array( 'status' => 400 ) );\n    }\n    // Cap the PDF size before it lands in the LONGTEXT column: a buggy push must not put a huge blob into\n    // the database. 8MB of raw PDF is far above any real service-charge demand.\n    if ( strlen( $raw ) > 8 * 1024 * 1024 ) {\n        return new WP_Error( 'pdf_too_large', 'Demand PDF exceeds the 8MB limit.', array( 'status' => 413 ) );\n    }\n    if ( $sha !== '' && ! hash_equals( strtolower( $sha ), hash( 'sha256', $raw ) ) ) {\n        return new WP_Error( 'bad_pdf_hash', 'PDF SHA-256 did not match.', array( 'status' => 400 ) );\n    }\n    global $wpdb;\n    $stored = $wpdb->replace( $wpdb->prefix . 'pc_rtm_pdfs', array(\n        'pdf_key' => $pdf_key, 'document_number' => $doc, 'flat_id' => $flat_id,\n        'mime' => 'application/pdf', 'content_b64' => $content,\n        'sha256' => $sha !== '' ? strtolower( $sha ) : hash( 'sha256', $raw ),\n        'created_at' => current_time( 'mysql', true ),\n    ) );\n    // Never report success to Mongoose if the write failed (e.g. a missing table): otherwise the\n    // leaseholder would silently have no downloadable demand while Mongoose believes it is published.\n    if ( $stored === false ) {\n        return new WP_Error( 'pdf_store_failed', 'Could not store the demand PDF.', array( 'status' => 500 ) );\n    }\n    return array( 'ok' => true, 'pdf_key' => $pdf_key );\n}\n\nfunction pc_rtm_can_download_demand_pdf( WP_REST_Request $req ) {\n    if ( pc_rtm_admin_permission() ) return true;\n    if ( ! is_user_logged_in() ) return false;\n    global $wpdb;\n    $key = sanitize_text_field( (string) $req['key'] );\n    $row = $wpdb->get_row( $wpdb->prepare(\n        \"SELECT flat_id FROM {$wpdb->prefix}pc_rtm_pdfs WHERE pdf_key=%s\", $key ), ARRAY_A );\n    if ( ! $row ) return false;\n    $mine = pc_rtm_portal_user_flat_id();\n    if ( $mine === '' || strcasecmp( (string) $row['flat_id'], $mine ) !== 0 ) return false;\n    // C1 (fail closed): the linked flat is not enough , the user must ALSO still be the current on-file\n    // leaseholder for it, so a sold-on owner cannot keep downloading the new owner's demand PDFs.\n    $u = wp_get_current_user();\n    if ( function_exists( 'pc_rtm_email_authorized_for_flat' )\n         && ! pc_rtm_email_authorized_for_flat( $u ? $u->user_email : '', $mine ) ) {\n        return false;\n    }\n    return true;\n}\n\n/**\n * M7: PDF lifecycle retention + orphan reconciliation.\n * The pc_rtm_pdfs store holds base64 demand/receipt PDFs pushed by Mongoose. Without reconciliation, a\n * superseded or replaced document's bytes rest in WordPress indefinitely and enlarge every backup. This\n * collects the pdf_keys the CURRENT snapshot still references (demands, receipts, and any per-flat\n * open_demands), then deletes stored rows that are BOTH unreferenced AND older than the retention window,\n * so a just-pushed receipt not yet reflected in a snapshot is never swept. Returns the number removed.\n */\nfunction pc_rtm_collect_snapshot_pdf_keys( $snap ) {\n    $keys = array();\n    if ( ! is_array( $snap ) ) return $keys;\n    foreach ( array( 'demands', 'receipts' ) as $list ) {\n        foreach ( (array) ( $snap[ $list ] ?? array() ) as $d ) {\n            if ( is_array( $d ) && ! empty( $d['pdf_key'] ) ) $keys[ (string) $d['pdf_key'] ] = true;\n        }\n    }\n    foreach ( (array) ( $snap['flats'] ?? array() ) as $f ) {\n        if ( ! is_array( $f ) ) continue;\n        foreach ( (array) ( $f['open_demands'] ?? array() ) as $d ) {\n            if ( is_array( $d ) && ! empty( $d['pdf_key'] ) ) $keys[ (string) $d['pdf_key'] ] = true;\n        }\n    }\n    return array_keys( $keys );\n}\n\nfunction pc_rtm_reconcile_pdfs( $snap = null ) {\n    global $wpdb;\n    $table = $wpdb->prefix . 'pc_rtm_pdfs';\n    if ( $wpdb->get_var( $wpdb->prepare( 'SHOW TABLES LIKE %s', $table ) ) !== $table ) return 0;\n    if ( $snap === null ) {\n        $snap = json_decode( (string) get_option( 'pc_rtm_snapshot', '' ), true );\n    }\n    // Retention window: an unreferenced PDF is only removed once it is older than this, so a receipt pushed\n    // and served before it appears in a snapshot survives. Filterable; default 180 days.\n    $retain = (int) apply_filters( 'pc_rtm_pdf_retention_age', 180 * DAY_IN_SECONDS );\n    $cutoff = gmdate( 'Y-m-d H:i:s', time() - $retain );\n    $referenced = pc_rtm_collect_snapshot_pdf_keys( $snap );\n    // Candidate orphans: rows older than the cutoff. Filter out the referenced keys in PHP (the referenced\n    // set is tiny for this building), then delete the remainder one row at a time (prepared).\n    $rows = $wpdb->get_col( $wpdb->prepare( \"SELECT pdf_key FROM `$table` WHERE created_at < %s\", $cutoff ) );\n    if ( ! $rows ) return 0;\n    $ref = array_fill_keys( array_map( 'strval', $referenced ), true );\n    $removed = 0;\n    foreach ( $rows as $key ) {\n        if ( isset( $ref[ (string) $key ] ) ) continue; // still referenced by the live snapshot\n        if ( $wpdb->delete( $table, array( 'pdf_key' => $key ) ) ) $removed++;\n    }\n    if ( $removed > 0 ) {\n        error_log( sprintf( 'pc_rtm PDF reconcile: removed %d orphaned PDF(s) older than the retention window.', $removed ) );\n    }\n    return $removed;\n}\n\n// M7: also reconcile on a daily schedule, so orphans are retired even during a quiet spell with no\n// snapshot pushes. Scheduled on init; cleared on deactivation alongside the other RTM crons.\nadd_action( 'init', 'pc_rtm_schedule_pdf_reconcile' );\nfunction pc_rtm_schedule_pdf_reconcile() {\n    if ( ! wp_next_scheduled( 'pc_rtm_reconcile_pdfs_cron' ) ) {\n        wp_schedule_event( time() + 2 * HOUR_IN_SECONDS, 'daily', 'pc_rtm_reconcile_pdfs_cron' );\n    }\n}\nadd_action( 'pc_rtm_reconcile_pdfs_cron', 'pc_rtm_reconcile_pdfs' );\n\nfunction pc_rtm_serve_demand_pdf( WP_REST_Request $req ) {\n    global $wpdb;\n    $key = sanitize_text_field( (string) $req['key'] );\n    $row = $wpdb->get_row( $wpdb->prepare(\n        \"SELECT * FROM {$wpdb->prefix}pc_rtm_pdfs WHERE pdf_key=%s\", $key ), ARRAY_A );\n    if ( ! $row ) return new WP_Error( 'not_found', 'PDF not found.', array( 'status' => 404 ) );\n    $raw = base64_decode( (string) $row['content_b64'], true );\n    if ( ! is_string( $raw ) ) return new WP_Error( 'bad_pdf', 'Stored PDF is invalid.', array( 'status' => 500 ) );\n    nocache_headers();\n    header( 'Content-Type: application/pdf' );\n    // M7: stop a browser MIME-sniffing the response into anything other than a PDF (defence in depth for a\n    // store that holds caller-supplied bytes, even though we validate the %PDF magic on ingest).\n    header( 'X-Content-Type-Options: nosniff' );\n    header( 'Content-Disposition: inline; filename=\"' . sanitize_file_name( $row['document_number'] . '.pdf' ) . '\"' );\n    header( 'Content-Length: ' . strlen( $raw ) );\n    echo $raw; // phpcs:ignore -- raw PDF body, headers set above\n    exit;\n}\n"


# The generated plugin carries the site's identity (organisation name, plugin slug, REST namespace, site
# domain, a sample flat id). None of it is a source literal: it is read from the site profile at the moment
# the PHP is generated, so a real profile produces the real plugin and the packaged fictional profile a
# fictional one. The main template is a str.format template (its PHP braces are doubled); the portal-sync
# template is plain text with {{profile.key}} placeholders filled by profile.render().
_PHP_SAFE_VALUE = re.compile(r"[A-Za-z0-9 .,&()/_-]+")


def plugin_identity() -> dict[str, str]:
    """Identity values baked into the generated plugin, from the active site profile. Refuses a value that
    would break the PHP it is written into (a quote or backslash inside a single-quoted PHP string)."""
    ident = {
        "rtm_name": _profile.rtm_name(),
        "plugin_slug": _profile.wp_plugin_slug(),
        "site_domain": _profile.site_domain(),
        "wp_namespace": _profile.wp_namespace(),
        "example_flat": f"{_profile.block_codes()[0]}F7",
    }
    bad = sorted(k for k, v in ident.items() if not _PHP_SAFE_VALUE.fullmatch(v or ""))
    if bad:
        raise ValueError("site profile values not safe to write into plugin PHP: " + ", ".join(bad))
    return ident


def plugin_main_php(version: str) -> str:
    """The generated main plugin file (<plugin_slug>.php) for this site."""
    return _EXAMPLE_COURT_PLUGIN_PHP.format(version=version, **plugin_identity())


def portal_sync_php() -> str:
    """The generated includes/portal-sync.php for this site (REST namespace, text domain, branding)."""
    plugin_identity()  # same safety check as the main file
    return _profile.render(_PORTAL_SYNC_PHP)


def refresh_scaffold(version: str = "0.2.0") -> dict[str, Any]:
    """Regenerate ONLY the generated files (main plugin php + includes/portal-sync.php) in an
    EXISTING plugin source dir, preserving everything else. This is how an already-scaffolded
    install picks up template improvements (e.g. the Service Charges / Communications pages
    replacing the old coming-soon stubs) without deleting the directory. The current files are
    backed up beside the source dir first, so nothing is lost."""
    import shutil as _sh
    from datetime import datetime as _dt
    src = _plugin_dir()
    if not src.exists():
        return {"ok": False, "reason": "no_source", "path": str(src),
                "hint": "Nothing to refresh; run scaffold first."}
    backup = src.parent / f"{src.name}-backup-{_dt.now().strftime('%Y%m%d-%H%M%S')}"
    try:
        _sh.copytree(src, backup)
    except Exception as exc:
        return {"ok": False, "reason": f"backup_failed: {exc}"}
    slug = _profile.wp_plugin_slug()
    php = plugin_main_php(version)
    main_file = src / f"{slug}.php"
    main_file.write_text(php, encoding="utf-8")
    with main_file.open("a", encoding="utf-8") as fh:
        fh.write("\n// Leaseholder portal sync API (pairs with Mongoose rtm_portal).\n")
        fh.write("require_once PC_RTM_PLUGIN_DIR . 'includes/portal-sync.php';\n")
    includes = src / "includes"
    includes.mkdir(exist_ok=True)
    (includes / "portal-sync.php").write_text(portal_sync_php(), encoding="utf-8")
    return {"ok": True, "path": str(src), "backup": str(backup),
            "refreshed": [str(main_file), str(includes / "portal-sync.php")],
            "next": "Build and deploy: the Service Charges and Communications pages are now real."}


def scaffold_example_court_plugin(version: str = "0.1.0") -> dict[str, Any]:
    """Create a starter RTM plugin for this site (identity from the site profile) in MONGOOSE_PLUGIN_DIR.
    Generates a working WordPress plugin with admin menu, a live Service Charges page
    (rendered from the Mongoose snapshot), a Communications page (draft/publish
    leaseholder notices), settings page (Mongoose URL), and the portal sync API.
    Safe to call when the directory doesn't exist yet: creates it.
    Returns {ok, path, files_created}."""
    src = _plugin_dir()
    if src.exists() and any(src.iterdir()):
        return {"ok": False, "reason": "directory_not_empty",
                "path": str(src),
                "hint": "MONGOOSE_PLUGIN_DIR already has files. Delete or rename it to scaffold fresh."}
    src.mkdir(parents=True, exist_ok=True)
    slug = _profile.wp_plugin_slug()
    php = plugin_main_php(version)
    main_file = src / f"{slug}.php"
    main_file.write_text(php, encoding="utf-8")
    # Portal sync API: the module Mongoose's rtm_portal pairs with (snapshot push,
    # encrypted claim pull, decisions, issue sync). Written separately and required
    # from the main file so the two halves stay readable.
    includes = src / "includes"
    includes.mkdir(exist_ok=True)
    (includes / "portal-sync.php").write_text(portal_sync_php(), encoding="utf-8")
    with main_file.open("a", encoding="utf-8") as fh:
        fh.write("\n// Leaseholder portal sync API (pairs with Mongoose rtm_portal).\n")
        fh.write("require_once PC_RTM_PLUGIN_DIR . 'includes/portal-sync.php';\n")
    # Minimal readme
    readme = src / "readme.txt"
    readme.write_text(
        f"=== {_profile.rtm_name()} RTM ===\nVersion: {version}\n\nRTM workflow plugin for {_profile.rtm_name()}.\n",
        encoding="utf-8"
    )
    # Assets stub
    assets = src / "assets"
    assets.mkdir(exist_ok=True)
    (assets / ".gitkeep").write_text("")
    files = [str(f.relative_to(src)) for f in src.rglob("*") if f.is_file()]
    return {"ok": True, "path": str(src), "slug": slug, "version": version,
            "files_created": files,
            "next_step": f"Edit {main_file.name} to build out the RTM features, then run: build plugin zip"}
