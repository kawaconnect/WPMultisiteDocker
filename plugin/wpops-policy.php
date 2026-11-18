<?php
/**
 * Plugin Name: wpops Deployment Policy
 * Description: Restore-independent per-blog login paths for WPS Hide Login, and no user listing for anonymous REST clients.
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

/* Anonymous visitors cannot list users over REST (/wp-json/wp/v2/users and ?rest_route=...): the answer is the
 * login names, which is all that password guessing needs. Signed-in users (the editor) and clients that
 * authenticate with an application password still get the endpoint. A site that publishes its authors through
 * REST on purpose sets define('WPOPS_ALLOW_ANON_REST_USERS', true) in wp-config.php. */
add_filter('rest_endpoints', static function ($endpoints) {
    if ((defined('WPOPS_ALLOW_ANON_REST_USERS') && WPOPS_ALLOW_ANON_REST_USERS) || is_user_logged_in()) {
        return $endpoints;
    }
    foreach (array_keys($endpoints) as $route) {
        if (0 === strpos($route, '/wp/v2/users')) {
            unset($endpoints[$route]);
        }
    }
    return $endpoints;
});

/* Subdomain networks: core gives a new blog http:// URLs (it only copies the
 * main site's https scheme for path-based networks), so blogs created with
 * WP-CLI or the network admin would link to http. Follow the main site. */
add_action('wp_initialize_site', static function ($site) {
    if (!is_subdomain_install()
            || 'https' !== wp_parse_url(get_home_url(get_main_site_id($site->network_id)), PHP_URL_SCHEME)) {
        return;
    }
    switch_to_blog((int) $site->blog_id);
    foreach (['home', 'siteurl'] as $option) {
        $url = get_option($option);
        if (is_string($url) && 0 === strpos($url, 'http://')) {
            update_option($option, 'https://' . substr($url, 7));
        }
    }
    restore_current_blog();
}, 20);
