<?php
/** Plugin Name: wpops Host Alias Loader */
if (!defined('ABSPATH')) {
    exit;
}
require_once __DIR__ . '/kc-host-alias/kc-host-alias.php';

/* Preserve site-provided MU plugins when using the external deployment directory. */
foreach (glob(WP_CONTENT_DIR . '/mu-plugins/*.php') ?: [] as $wpops_mu) {
    if (!in_array(basename($wpops_mu), ['kc-host-alias.php', 'wpops-policy.php', 'wpops-host-alias-loader.php'], true)) {
        include_once $wpops_mu;
    }
}
unset($wpops_mu);
