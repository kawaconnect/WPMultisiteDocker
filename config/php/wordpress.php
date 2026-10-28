<?php
/* Shared by FPM and WP-CLI. Values come from this stack's environment. */
define('WPMU_PLUGIN_DIR', '/etc/wpops/mu-plugins');
$wpops_mode = getenv('WP_MODE') ?: 'multisite';
if ($wpops_mode === 'multisite') {
    define('WP_ALLOW_MULTISITE', true);
    $wpops_report = mysqli_report(MYSQLI_REPORT_OFF);
    try {
        $wpops_db = new mysqli(explode(':', DB_HOST)[0], DB_USER, DB_PASSWORD, DB_NAME);
        if (!$wpops_db->connect_error) {
            $wpops_prefix = isset($table_prefix) ? $table_prefix : 'wp_';
            $wpops_table = $wpops_db->real_escape_string(addcslashes($wpops_prefix . 'blogs', '_%'));
            $wpops_result = $wpops_db->query("SHOW TABLES LIKE '$wpops_table'");
            if ($wpops_result && $wpops_result->num_rows > 0) {
                define('MULTISITE', true);
                define('SUBDOMAIN_INSTALL', getenv('WP_SUBDOMAIN_INSTALL') !== 'false');
                define('DOMAIN_CURRENT_SITE', getenv('WP_DOMAIN_CURRENT_SITE'));
                define('PATH_CURRENT_SITE', '/');
                define('SITE_ID_CURRENT_SITE', 1);
                define('BLOG_ID_CURRENT_SITE', 1);
                if (getenv('WP_SUNRISE') === 'true') {
                    define('SUNRISE', true); /* WordPress loads sunrise.php whenever SUNRISE is defined */
                }
            }
            $wpops_db->close();
        }
    } finally {
        mysqli_report($wpops_report);
    }
}
define('WP_REDIS_HOST', 'redis');
define('WP_REDIS_PORT', 6379);
define('WP_REDIS_PASSWORD', getenv('REDIS_PASSWORD'));
define('DISABLE_WP_CRON', true);
if (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https') {
    $_SERVER['HTTPS'] = 'on';
}
unset($wpops_mode, $wpops_report, $wpops_db, $wpops_prefix, $wpops_table, $wpops_result);
