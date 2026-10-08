<?php
/**
 * Plugin Name: Example Court Leaseholder Portal
 * Plugin URI:  https://example-court.org
 * Description: Secure leaseholder portal for Example Court RTM Company Limited.
 *              Provides registration/claim, dashboard, demand PDF download, account statement,
 *              and issue submission for the 18 flats at 1 and 3 Example Court.
 *              Authentication gateway: Mongoose validates all registration claims.
 *              This plugin is display-only: it does not take payments, confirm payments,
 *              create ledger entries, or alter bank reconciliation data.
 * Version:     0.1.0-skeleton
 * Author:      Example Court RTM Company Limited
 * License:     Private
 * Text Domain: example-court-portal
 *
 * ARCHITECTURE: Snapshot-push (spec §2)
 *   Mongoose → builds portal-safe snapshot → pushes to this plugin → leaseholders view their data
 *   Registration claims → encrypted here → pulled by Mongoose → validated locally → decision pushed back
 *   Leaseholder browsers never call Mongoose directly.
 *
 * STATUS: SKELETON — all endpoints and shortcodes are stubbed.
 * STATIC REFERENCE: names, namespace and text domain match the packaged fictional site profile.
 *   Mongoose never reads or deploys this file; the plugin it deploys is generated from the active
 *   site profile by src/mongoose/services/wp_plugin.py (plugin_main_php / portal_sync_php).
 * See: docs/RTM_PORTAL_SPEC.md for the full specification.
 */

if ( ! defined( 'ABSPATH' ) ) exit;

define( 'PC_PORTAL_VERSION',  '0.1.0-skeleton' );
define( 'PC_PORTAL_DB_VERSION', 1 );
define( 'PC_PORTAL_PLUGIN_DIR', plugin_dir_path( __FILE__ ) );
define( 'PC_PORTAL_REST_NS',  'example-court/v1' );

// ── Activation / deactivation ──────────────────────────────────────────────

register_activation_hook( __FILE__, 'pc_portal_activate' );
function pc_portal_activate(): void {
    pc_portal_create_tables();
    pc_portal_create_role();
    add_option( 'pc_portal_db_version', PC_PORTAL_DB_VERSION );
    add_option( 'pc_portal_public_key_b64', '' ); // set by Mongoose after keypair generation
    flush_rewrite_rules();
}

register_deactivation_hook( __FILE__, 'pc_portal_deactivate' );
function pc_portal_deactivate(): void {
    flush_rewrite_rules();
    // Tables preserved on deactivate; removed only on uninstall
}

// ── Database tables (spec §15) ──────────────────────────────────────────────

function pc_portal_create_tables(): void {
    global $wpdb;
    $charset_collate = $wpdb->get_charset_collate();
    require_once ABSPATH . 'wp-admin/includes/upgrade.php';

    // wp_pc_portal_flats (spec §15.1)
    dbDelta( "CREATE TABLE {$wpdb->prefix}pc_portal_flats (
        id                      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        flat_id                 VARCHAR(16)  NOT NULL UNIQUE,
        block                   VARCHAR(8)   NOT NULL,
        flat_number             VARCHAR(8)   NOT NULL,
        claim_enabled           TINYINT(1)   NOT NULL DEFAULT 1,
        safe_status_json        TEXT,
        statement_json          LONGTEXT,
        balance_pence           BIGINT       DEFAULT NULL,
        position                VARCHAR(16)  DEFAULT NULL,
        payment_reference_display VARCHAR(64) DEFAULT NULL,
        last_snapshot_at        DATETIME     DEFAULT NULL,
        last_reconciled_at      DATETIME     DEFAULT NULL,
        created_at              DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at              DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (id)
    ) $charset_collate;" );

    // wp_pc_portal_demands (spec §15.2)
    // NOTE: document_number is NOT exposed as a claim verifier (spec §15.2)
    dbDelta( "CREATE TABLE {$wpdb->prefix}pc_portal_demands (
        id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        flat_id             VARCHAR(16)  NOT NULL,
        document_number     VARCHAR(64)  NOT NULL,
        period              VARCHAR(32)  DEFAULT NULL,
        issue_date          DATE         DEFAULT NULL,
        due_date            DATE         DEFAULT NULL,
        amount_pence        BIGINT       NOT NULL DEFAULT 0,
        allocated_pence     BIGINT       NOT NULL DEFAULT 0,
        outstanding_pence   BIGINT       NOT NULL DEFAULT 0,
        status              VARCHAR(32)  DEFAULT NULL,
        pdf_key             VARCHAR(64)  DEFAULT NULL,
        pdf_sha256          VARCHAR(64)  DEFAULT NULL,
        created_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        INDEX idx_flat_id (flat_id),
        UNIQUE KEY uidx_document (document_number)
    ) $charset_collate;" );
    // IMPORTANT: pdf_storage_path is NOT stored in WordPress (spec §9.3, §15.2)

    // wp_pc_portal_pending_claims (spec §15.3)
    // Raw payment reference and document number are NEVER stored here (spec §5.1, §15.3)
    dbDelta( "CREATE TABLE {$wpdb->prefix}pc_portal_pending_claims (
        id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        public_claim_ref    VARCHAR(64)  NOT NULL UNIQUE,
        encrypted_payload   LONGTEXT     NOT NULL,
        safe_metadata_json  TEXT,
        status              VARCHAR(32)  NOT NULL DEFAULT 'pending',
        rate_limit_bucket   VARCHAR(128) DEFAULT NULL,
        ip_hash             VARCHAR(64)  DEFAULT NULL,
        created_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        pulled_at           DATETIME     DEFAULT NULL,
        decided_at          DATETIME     DEFAULT NULL,
        decision_json       TEXT         DEFAULT NULL,
        PRIMARY KEY (id),
        INDEX idx_status (status),
        INDEX idx_created (created_at)
    ) $charset_collate;" );

    // wp_pc_portal_claims (spec §15.4)
    dbDelta( "CREATE TABLE {$wpdb->prefix}pc_portal_claims (
        id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        flat_id             VARCHAR(16)  DEFAULT NULL,
        user_id             BIGINT UNSIGNED DEFAULT NULL,
        email               VARCHAR(255) NOT NULL,
        first_name          VARCHAR(128) DEFAULT NULL,
        surname             VARCHAR(128) DEFAULT NULL,
        status              VARCHAR(32)  NOT NULL DEFAULT 'pending_claim',
        name_match_status   VARCHAR(32)  DEFAULT NULL,
        email_match_status  VARCHAR(32)  DEFAULT NULL,
        claimed_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        email_verified_at   DATETIME     DEFAULT NULL,
        reviewed_at         DATETIME     DEFAULT NULL,
        reviewed_by         BIGINT UNSIGNED DEFAULT NULL,
        notes               TEXT         DEFAULT NULL,
        PRIMARY KEY (id),
        INDEX idx_flat_id (flat_id),
        INDEX idx_user_id (user_id),
        INDEX idx_status (status)
    ) $charset_collate;" );

    // wp_pc_portal_issues (spec §15.5)
    dbDelta( "CREATE TABLE {$wpdb->prefix}pc_portal_issues (
        id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        public_reference    VARCHAR(32)  NOT NULL UNIQUE,
        flat_id             VARCHAR(16)  NOT NULL,
        user_id             BIGINT UNSIGNED DEFAULT NULL,
        category            VARCHAR(32)  NOT NULL,
        title               VARCHAR(255) NOT NULL,
        description         LONGTEXT     NOT NULL,
        location            VARCHAR(255) DEFAULT NULL,
        urgency             VARCHAR(16)  NOT NULL DEFAULT 'routine',
        status              VARCHAR(32)  NOT NULL DEFAULT 'open',
        submitted_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        mongoose_case_id    VARCHAR(64)  DEFAULT NULL,
        sync_status         VARCHAR(16)  DEFAULT 'pending',
        sync_error          TEXT         DEFAULT NULL,
        PRIMARY KEY (id),
        INDEX idx_flat_id (flat_id),
        INDEX idx_user_id (user_id),
        INDEX idx_sync (sync_status)
    ) $charset_collate;" );

    // wp_pc_portal_audit (spec §15.6)
    dbDelta( "CREATE TABLE {$wpdb->prefix}pc_portal_audit (
        id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        actor_user_id       BIGINT UNSIGNED DEFAULT NULL,
        action              VARCHAR(64)  NOT NULL,
        target_type         VARCHAR(32)  DEFAULT NULL,
        target_id           VARCHAR(64)  DEFAULT NULL,
        ip_hash             VARCHAR(64)  DEFAULT NULL,
        metadata_json       TEXT         DEFAULT NULL,
        created_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        INDEX idx_actor (actor_user_id),
        INDEX idx_action (action),
        INDEX idx_created (created_at)
    ) $charset_collate;" );

    // wp_pc_portal_sync_runs
    dbDelta( "CREATE TABLE {$wpdb->prefix}pc_portal_sync_runs (
        id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        sync_type           VARCHAR(32)  NOT NULL,
        status              VARCHAR(16)  NOT NULL DEFAULT 'ok',
        flat_count          INT          DEFAULT 0,
        notes               TEXT         DEFAULT NULL,
        ran_at              DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        INDEX idx_sync_type (sync_type)
    ) $charset_collate;" );
}

// ── Role ──────────────────────────────────────────────────────────────────────

function pc_portal_create_role(): void {
    add_role( 'example_court_leaseholder', 'Example Court Leaseholder', [
        'read' => true,
    ] );
}

// ── Shortcodes (stubs) ─────────────────────────────────────────────────────

add_shortcode( 'example_court_leaseholder_portal', 'pc_portal_shortcode_portal' );
function pc_portal_shortcode_portal( array $atts ): string {
    if ( ! is_user_logged_in() ) {
        return pc_portal_registration_form();
    }
    return pc_portal_dashboard();
}

add_shortcode( 'example_court_issue_form', 'pc_portal_shortcode_issue_form' );
function pc_portal_shortcode_issue_form( array $atts ): string {
    if ( ! pc_portal_is_leaseholder() ) {
        return '<p>You must be logged in as a registered leaseholder to submit an issue.</p>';
    }
    return pc_portal_issue_form();
}

add_shortcode( 'example_court_account_statement', 'pc_portal_shortcode_statement' );
function pc_portal_shortcode_statement( array $atts ): string {
    if ( ! pc_portal_is_leaseholder() ) {
        return '<p>You must be logged in as a registered leaseholder to view your account.</p>';
    }
    return pc_portal_account_statement();
}

// ── Leaseholder check ──────────────────────────────────────────────────────

function pc_portal_is_leaseholder(): bool {
    if ( ! is_user_logged_in() ) return false;
    $user = wp_get_current_user();
    return in_array( 'example_court_leaseholder', (array) $user->roles, true )
        || current_user_can( 'manage_options' );
}

function pc_portal_current_flat_ids(): array {
    $user_id = get_current_user_id();
    $ids = get_user_meta( $user_id, 'pc_flat_ids', true );
    return is_array( $ids ) ? $ids : ( $ids ? [ $ids ] : [] );
}

// ── Registration / claim form (stub) ──────────────────────────────────────

function pc_portal_registration_form(): string {
    // STUB: render a registration form that collects name, email, password,
    // payment reference and document number.
    // On submit: encrypt claim payload with sodium_crypto_box_seal() using the
    // public key from pc_portal_public_key_b64 option, store in pending_claims table,
    // create pending WP user, redirect to "check your email" page.
    // NEVER store raw payment reference or document number (spec §5.1, §15.3).
    ob_start(); ?>
    <div class="pc-portal-notice">
        <p><strong>Leaseholder registration — coming soon.</strong></p>
        <p>This portal is being set up for Example Court leaseholders. 
           To register you will need your payment reference and service charge demand document number 
           exactly as shown on your demand letter.</p>
        <!-- DELTA: implement registration form with claim encryption -->
    </div>
    <?php return ob_get_clean();
}

// ── Dashboard (stub) ──────────────────────────────────────────────────────

function pc_portal_dashboard(): string {
    $flat_ids = pc_portal_current_flat_ids();
    ob_start(); ?>
    <div class="pc-portal-dashboard">
        <h2>Your leaseholder dashboard</h2>
        <?php if ( empty( $flat_ids ) ): ?>
        <p class="pc-notice">Your account is pending activation. You will receive an email when access is ready.</p>
        <?php else: ?>
        <p>Flat(s): <?php echo esc_html( implode( ', ', $flat_ids ) ); ?></p>
        <!-- DELTA: render demand PDF links, bank details, account statement summary, issue form link -->
        <?php endif; ?>
        <!-- Non-goal notice (spec §4) -->
        <div class="pc-portal-notice pc-notice-info">
            <strong>This portal does not take payments.</strong>
            To pay your service charge, use the bank details shown below with your exact payment reference.
        </div>
    </div>
    <?php return ob_get_clean();
}

// ── Account statement (stub) ──────────────────────────────────────────────

function pc_portal_account_statement(): string {
    // Spec §11: caveat and last-reconciled display
    $flat_ids = pc_portal_current_flat_ids();
    if ( empty( $flat_ids ) ) return '<p>No flat linked to your account.</p>';
    $flat_id  = $flat_ids[0];
    global $wpdb;
    $flat = $wpdb->get_row(
        $wpdb->prepare( "SELECT * FROM {$wpdb->prefix}pc_portal_flats WHERE flat_id=%s", $flat_id ),
        ARRAY_A
    );
    ob_start();
    // 7-day caveat (exact wording from spec §3.4 / §11)
    echo '<div class="pc-caveat">';
    echo '<p>Payment records are reconciled manually from the RTM bank account. '
       . 'The portal can take up to 7 days to be fully up to date after you make a payment. '
       . 'If you paid more than 7 days ago and it is not shown, raise a payment issue and '
       . 'include the payment date, amount and reference used.</p>';
    echo '</div>';
    if ( $flat ) {
        $last_rec = $flat['last_reconciled_at'] ?? null;
        echo '<p>Last reconciled: ' . ( $last_rec ? esc_html( $last_rec ) : 'not yet available' ) . '</p>';
        if ( $last_rec ) {
            $age_days = (int) ( ( time() - strtotime( $last_rec ) ) / 86400 );
            if ( $age_days > 7 ) {
                echo '<p class="pc-warn">This account information may be older than usual because the latest reconciliation is more than 7 days old.</p>';
            }
        }
        $statement = json_decode( $flat['statement_json'] ?? '{}', true ) ?: [];
        // DELTA: render statement table (Date, Description, Debit, Credit, Running balance, Reference)
        echo '<p class="pc-muted">Statement data will appear here once the portal sync is active.</p>';
    }
    return ob_get_clean();
}

// ── Issue form (stub) ─────────────────────────────────────────────────────

function pc_portal_issue_form(): string {
    // Spec §12: categories, fields, payment issue fields (no ledger alteration)
    ob_start(); ?>
    <div class="pc-issue-form">
        <h3>Raise an issue</h3>
        <!-- DELTA: implement issue submission form with categories:
             building, payment, document, portal, other.
             Payment issues: include date/amount/reference fields.
             On submit: create issue in wp_pc_portal_issues, queue for Mongoose sync.
             CRITICAL: payment issues do NOT alter the ledger (spec §12.3). -->
        <p class="pc-muted">Issue submission form coming soon.</p>
    </div>
    <?php return ob_get_clean();
}

// ── REST endpoints (stubs) ──────────────────────────────────────────────────

add_action( 'rest_api_init', 'pc_portal_register_rest_routes' );
function pc_portal_register_rest_routes(): void {
    // Public, rate-limited (spec §16.1)
    register_rest_route( PC_PORTAL_REST_NS, '/claim/start', [
        'methods'             => 'POST',
        'callback'            => 'pc_portal_rest_claim_start',
        'permission_callback' => '__return_true',
    ] );
    register_rest_route( PC_PORTAL_REST_NS, '/claim/email-verify', [
        'methods'             => 'POST',
        'callback'            => 'pc_portal_rest_email_verify',
        'permission_callback' => '__return_true',
    ] );

    // Protected Mongoose sync (spec §16.3) — requires PC_PORTAL_SYNC_SECRET header
    register_rest_route( PC_PORTAL_REST_NS, '/sync/snapshot', [
        'methods'             => 'POST',
        'callback'            => 'pc_portal_rest_receive_snapshot',
        'permission_callback' => 'pc_portal_auth_sync',
    ] );
    register_rest_route( PC_PORTAL_REST_NS, '/sync/pending-claims', [
        'methods'             => 'GET',
        'callback'            => 'pc_portal_rest_get_pending_claims',
        'permission_callback' => 'pc_portal_auth_sync',
    ] );
    register_rest_route( PC_PORTAL_REST_NS, '/sync/claim-decision/(?P<id>[\d]+)', [
        'methods'             => 'PATCH',
        'callback'            => 'pc_portal_rest_claim_decision',
        'permission_callback' => 'pc_portal_auth_sync',
    ] );
    register_rest_route( PC_PORTAL_REST_NS, '/sync/issues', [
        'methods'             => 'GET',
        'callback'            => 'pc_portal_rest_get_issues',
        'permission_callback' => 'pc_portal_auth_sync',
    ] );
    register_rest_route( PC_PORTAL_REST_NS, '/sync/readiness', [
        'methods'             => 'GET',
        'callback'            => 'pc_portal_rest_readiness',
        'permission_callback' => 'pc_portal_auth_sync',
    ] );
}

// ── Sync authentication ────────────────────────────────────────────────────

function pc_portal_auth_sync( WP_REST_Request $request ): bool {
    // DELTA: verify X-Mongoose-Signature HMAC against MONGOOSE_WEBHOOK_SECRET
    // (same signing scheme as the general Mongoose webhook — spec §16.3)
    $secret = get_option( 'pc_portal_sync_secret', '' );
    if ( ! $secret ) return false;
    $sig    = $request->get_header( 'X-Mongoose-Signature' ) ?? '';
    $body   = $request->get_body();
    $expected = 'sha256=' . hash_hmac( 'sha256', $body, $secret );
    return hash_equals( $expected, $sig );
}

// ── Stub REST handlers ─────────────────────────────────────────────────────

function pc_portal_rest_claim_start( WP_REST_Request $r ): WP_REST_Response {
    // DELTA: rate-limit by IP + global + per-email (spec §5.7)
    // Encrypt claim payload with sodium_crypto_box_seal (spec §6.1)
    // Store in pending_claims with only safe_metadata (NO raw refs — spec §5.1)
    // Return {status: "pending", message: "...check your email..."}
    return new WP_REST_Response( [
        'status'  => 'stub',
        'message' => 'Registration claim submission coming soon.',
    ], 200 );
}

function pc_portal_rest_email_verify( WP_REST_Request $r ): WP_REST_Response {
    return new WP_REST_Response( [ 'status' => 'stub' ], 200 );
}

function pc_portal_rest_receive_snapshot( WP_REST_Request $r ): WP_REST_Response {
    // DELTA: validate HMAC, parse snapshot JSON, upsert into wp_pc_portal_flats and
    // wp_pc_portal_demands. NEVER store pdf_storage_path (spec §9.3).
    // Run _assert_no_excluded_fields equivalent before storing.
    $body = $r->get_json_params();
    return new WP_REST_Response( [
        'status'      => 'stub',
        'flat_count'  => count( $body['flats'] ?? [] ),
        'accepted_at' => gmdate( 'c' ),
    ], 200 );
}

function pc_portal_rest_get_pending_claims( WP_REST_Request $r ): WP_REST_Response {
    // DELTA: return pending claims for Mongoose to pull + decrypt
    return new WP_REST_Response( [ 'status' => 'stub', 'pending' => [] ], 200 );
}

function pc_portal_rest_claim_decision( WP_REST_Request $r ): WP_REST_Response {
    // DELTA: apply Mongoose decision (approve/director_review/reject) to claim + WP user
    // Valid decisions: approve_email_verification_required, director_review_required, reject
    return new WP_REST_Response( [ 'status' => 'stub' ], 200 );
}

function pc_portal_rest_get_issues( WP_REST_Request $r ): WP_REST_Response {
    return new WP_REST_Response( [ 'status' => 'stub', 'issues' => [] ], 200 );
}

function pc_portal_rest_readiness( WP_REST_Request $r ): WP_REST_Response {
    global $wpdb;
    $has_sodium = function_exists( 'sodium_crypto_box_seal' );
    $public_key = get_option( 'pc_portal_public_key_b64', '' );
    $bank_name  = get_option( 'pc_rtm_bank_name', get_option( 'RTM_PORTAL_BANK_NAME', '' ) );
    return new WP_REST_Response( [
        'ok'               => $has_sodium && (bool) $public_key && (bool) $bank_name,
        'libsodium'        => $has_sodium,
        'claim_pubkey_set' => (bool) $public_key,
        'bank_name_set'    => (bool) $bank_name,
        'version'          => PC_PORTAL_VERSION,
    ], 200 );
}

// ── PDF protected download (stub) ─────────────────────────────────────────

add_action( 'init', 'pc_portal_register_download_rewrite' );
function pc_portal_register_download_rewrite(): void {
    add_rewrite_rule( '^pc-portal-download/([a-zA-Z0-9_-]+)/?$',
                      'index.php?pc_portal_download=1&pc_pdf_key=$matches[1]', 'top' );
    add_filter( 'query_vars', function( $vars ) {
        $vars[] = 'pc_portal_download';
        $vars[] = 'pc_pdf_key';
        return $vars;
    } );
}

add_action( 'template_redirect', 'pc_portal_handle_download' );
function pc_portal_handle_download(): void {
    if ( ! get_query_var( 'pc_portal_download' ) ) return;
    $pdf_key = sanitize_text_field( get_query_var( 'pc_pdf_key' ) );

    // Auth checks (spec §9.3)
    if ( ! is_user_logged_in() || ! pc_portal_is_leaseholder() ) {
        status_header( 403 ); exit( 'Access denied.' );
    }
    if ( ! $pdf_key ) {
        status_header( 400 ); exit( 'Invalid request.' );
    }
    // DELTA: verify pdf_key belongs to this user's flat, sha256 hash-check file,
    // stream bytes through PHP — NEVER redirect to a file URL (spec §9.3)
    status_header( 501 );
    exit( 'PDF download stub — not yet implemented.' );
}

// ── Admin: Director review screen (stub) ──────────────────────────────────

add_action( 'admin_menu', 'pc_portal_admin_menu' );
function pc_portal_admin_menu(): void {
    add_menu_page( 'RTM Portal', 'RTM Portal', 'manage_options',
                   'pc-portal-admin', 'pc_portal_admin_page',
                   'dashicons-id-alt', 25 );
    add_submenu_page( 'pc-portal-admin', 'Claim Review', 'Claim Review',
                      'manage_options', 'pc-portal-claims', 'pc_portal_admin_claims' );
    add_submenu_page( 'pc-portal-admin', 'Issues', 'Issues',
                      'manage_options', 'pc-portal-issues', 'pc_portal_admin_issues' );
    add_submenu_page( 'pc-portal-admin', 'Portal Status', 'Status',
                      'manage_options', 'pc-portal-status', 'pc_portal_admin_status' );
    add_submenu_page( 'pc-portal-admin', 'Settings', 'Settings',
                      'manage_options', 'pc-portal-settings', 'pc_portal_admin_settings' );
}

function pc_portal_admin_page(): void {
    echo '<div class="wrap"><h1>Example Court Leaseholder Portal</h1>';
    echo '<p>Use the sub-menu to manage claim reviews, issues, and portal configuration.</p>';
    echo '<p><em>Skeleton — admin screens to be implemented.</em></p></div>';
}

function pc_portal_admin_claims(): void {
    // DELTA: director review screen (spec §5.4):
    // flat_id, submitted name, submitted email, claim timestamp, claim match status,
    // name_match_status, email_match_status, existing active accounts for flat,
    // previous failed attempts. Approve (→ email verification gate) or Reject buttons.
    echo '<div class="wrap"><h1>Claim Review</h1>';
    echo '<p>Director review screen — DELTA: implement claim review table.</p></div>';
}

function pc_portal_admin_issues(): void {
    echo '<div class="wrap"><h1>Leaseholder Issues</h1>';
    echo '<p>Issue list — DELTA: render from wp_pc_portal_issues.</p></div>';
}

function pc_portal_admin_status(): void {
    // DELTA: show portal readiness (mirrors Mongoose rtm portal readiness)
    $rest_url = rest_url( PC_PORTAL_REST_NS . '/sync/readiness' );
    echo '<div class="wrap"><h1>Portal Status</h1>';
    echo '<p>Readiness check — DELTA: call /sync/readiness and render checks.</p>';
    echo '<p>REST base: <code>' . esc_html( rest_url( PC_PORTAL_REST_NS ) ) . '</code></p>';
    echo '</div>';
}

function pc_portal_admin_settings(): void {
    if ( isset( $_POST['pc_portal_settings_save'] ) && check_admin_referer( 'pc_portal_settings' ) ) {
        update_option( 'pc_rtm_bank_name',      sanitize_text_field( $_POST['bank_name'] ?? '' ) );
        update_option( 'pc_rtm_account_name',   sanitize_text_field( $_POST['account_name'] ?? '' ) );
        update_option( 'pc_rtm_sort_code',      sanitize_text_field( $_POST['sort_code'] ?? '' ) );
        update_option( 'pc_rtm_account_number', sanitize_text_field( $_POST['account_number'] ?? '' ) );
        update_option( 'pc_portal_sync_secret', sanitize_text_field( $_POST['sync_secret'] ?? '' ) );
        update_option( 'pc_portal_public_key_b64', sanitize_text_field( $_POST['public_key_b64'] ?? '' ) );
        echo '<div class="notice notice-success"><p>Settings saved.</p></div>';
    }
    ?>
    <div class="wrap"><h1>Portal Settings</h1>
    <form method="post">
    <?php wp_nonce_field( 'pc_portal_settings' ); ?>
    <h2>Bank details (displayed to leaseholders)</h2>
    <table class="form-table">
        <tr><th>Bank name</th><td><input name="bank_name" value="<?php echo esc_attr(get_option('pc_rtm_bank_name','')); ?>" class="regular-text"></td></tr>
        <tr><th>Account name</th><td><input name="account_name" value="<?php echo esc_attr(get_option('pc_rtm_account_name','')); ?>" class="regular-text"></td></tr>
        <tr><th>Sort code</th><td><input name="sort_code" value="<?php echo esc_attr(get_option('pc_rtm_sort_code','')); ?>" class="regular-text" placeholder="00-00-00"></td></tr>
        <tr><th>Account number</th><td><input name="account_number" value="<?php echo esc_attr(get_option('pc_rtm_account_number','')); ?>" class="regular-text"></td></tr>
    </table>
    <h2>Mongoose sync</h2>
    <table class="form-table">
        <tr><th>Sync secret (from Mongoose)</th><td><input name="sync_secret" type="password" value="<?php echo esc_attr(get_option('pc_portal_sync_secret','')); ?>" class="regular-text"><p class="description">Paste from: Mongoose cockpit → Settings → Webhook → Generate secret</p></td></tr>
        <tr><th>Claim public key (from Mongoose)</th><td><textarea name="public_key_b64" class="large-text" rows="3"><?php echo esc_textarea(get_option('pc_portal_public_key_b64','')); ?></textarea><p class="description">Paste from: mongoose rtm portal generate-keypair → public_key_b64</p></td></tr>
    </table>
    <p><input type="submit" name="pc_portal_settings_save" class="button-primary" value="Save settings"></p>
    </form></div>
    <?php
}
