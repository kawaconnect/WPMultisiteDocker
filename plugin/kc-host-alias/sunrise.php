<?php
/**
 * KC Host Alias, sunrise.php for multisite networks.
 *
 * Copy to wp-content/sunrise.php and set define('SUNRISE', true) (WP_SUNRISE=true
 * in .env for this repo). Runs before WordPress resolves the current blog from
 * wp_blogs: when the request Host is an alias from wpops-host-aliases.json with
 * a blog_id, that blog is selected directly. Everything else (URLs, cookies,
 * output rewriting) is done by mu-plugins/kc-host-alias.php.
 */

if (!defined('ABSPATH')) {
    exit;
}

(function () {
    global $wpdb, $current_blog, $current_site, $blog_id, $site_id, $domain, $path, $public;

    $file = WP_CONTENT_DIR . '/wpops-host-aliases.json';
    if (!is_readable($file)) {
        return;
    }
    $data = json_decode((string) file_get_contents($file), true);
    if (!is_array($data) || empty($data['aliases']) || !is_array($data['aliases'])) {
        return;
    }
    $host = isset($_SERVER['HTTP_HOST']) ? strtolower(preg_replace('/:\d+$/', '', (string) $_SERVER['HTTP_HOST'])) : '';
    if ($host === '' || empty($data['aliases'][$host]['blog_id'])) {
        return;
    }
    $target_blog = (int) $data['aliases'][$host]['blog_id'];

    $blog = $wpdb->get_row($wpdb->prepare("SELECT * FROM {$wpdb->blogs} WHERE blog_id = %d LIMIT 1", $target_blog));
    if (!$blog) {
        return;
    }
    $network = $wpdb->get_row($wpdb->prepare("SELECT * FROM {$wpdb->site} WHERE id = %d LIMIT 1", (int) $blog->site_id));
    if (!$network) {
        return;
    }

    $current_blog = $blog;
    $current_site = $network;
    $blog_id = (int) $blog->blog_id;
    $site_id = (int) $blog->site_id;
    $domain = $blog->domain;
    $path = $blog->path;
    $public = $blog->public;
    $wpdb->set_blog_id($blog_id, $site_id);

    if (!defined('COOKIE_DOMAIN')) {
        define('COOKIE_DOMAIN', $host);
    }
    if (!defined('DOMAIN_CURRENT_SITE')) {
        define('DOMAIN_CURRENT_SITE', $network->domain);
    }
    if (!defined('PATH_CURRENT_SITE')) {
        define('PATH_CURRENT_SITE', $network->path);
    }
    if (!defined('SITE_ID_CURRENT_SITE')) {
        define('SITE_ID_CURRENT_SITE', (int) $network->id);
    }
    if (!defined('BLOG_ID_CURRENT_SITE')) {
        define('BLOG_ID_CURRENT_SITE', (int) $network->blog_id);
    }
})();
