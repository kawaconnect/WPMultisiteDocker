<?php
/**
 * Plugin Name: wpops Deployment Policy
 * Description: Restore-independent per-blog login paths for WPS Hide Login.
 */
if (!defined('ABSPATH')) {
    exit;
}

function wpops_login_policy() {
    static $policy = null;
    if ($policy === null) {
        $file = defined('WPOPS_LOGIN_POLICY_FILE') ? WPOPS_LOGIN_POLICY_FILE : '/etc/wpops/policy/login-paths.json';
        $policy = is_readable($file) ? json_decode(file_get_contents($file), true) : [];
        $policy = is_array($policy) ? $policy : [];
    }
    return $policy;
}

function wpops_login_slug($original) {
    $policy = wpops_login_policy();
    $blog = (string) get_current_blog_id();
    $slug = $policy['blogs'][$blog] ?? $policy['default'] ?? '';
    return $slug !== '' ? $slug : $original;
}
add_filter('pre_option_whl_page', 'wpops_login_slug');
add_filter('pre_site_option_whl_page', 'wpops_login_slug');

/* Keep the plugin active after a database restore without modifying the source DB. */
function wpops_login_plugins($plugins) {
    if (!is_multisite() && !empty(wpops_login_policy()['enabled'])) {
        $plugins[] = 'wps-hide-login/wps-hide-login.php';
    }
    return array_values(array_unique($plugins));
}
add_filter('option_active_plugins', 'wpops_login_plugins');
add_filter('site_option_active_sitewide_plugins', static function ($plugins) {
    if (!empty(wpops_login_policy()['enabled'])) {
        $plugins['wps-hide-login/wps-hide-login.php'] = 1;
    }
    return $plugins;
});
