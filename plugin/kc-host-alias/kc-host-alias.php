<?php
/**
 * Plugin Name: KC Host Alias
 * Description: Serve a site under an alias hostname (a production domain during failover) without touching the database. Reads wp-content/wpops-host-aliases.json written by the wpops agent.
 * Version: 1.0.0
 * Author: KawaConnect
 *
 * Install as a must-use plugin: wp-content/mu-plugins/kc-host-alias.php.
 * For multisite also copy sunrise.php to wp-content/sunrise.php and set
 * WP_SUNRISE=true (define('SUNRISE', true)) so the blog is resolved before
 * WordPress looks the hostname up in wp_blogs.
 *
 * Map format (wp-content/wpops-host-aliases.json):
 *   {"aliases": {"example-shop.com": {"blog_id": 11, "host": "example-shop.standby.example.net"},
 *                "example-two.net": {"host": "example-two.standby.example.net"}}}
 * blog_id is set for multisite stacks; single-site stacks only carry host.
 *
 * What it does when the request Host is an alias:
 *   - home/siteurl resolve to https://<alias> (so every URL WordPress builds uses it)
 *   - COOKIE_DOMAIN is the alias (logins work on the alias)
 *   - the response body is rewritten so hard-coded links to the internal host
 *     (https://x.standby.example.net, //x.standby.example.net) point at the alias
 *   - REST/JSON responses get the same rewrite
 *   - the request is marked HTTPS when it arrived through the tunnel/proxy
 * Nothing is written to the database; removing the alias restores the old behaviour.
 */

if (!defined('ABSPATH')) {
    exit;
}

final class KC_Host_Alias {
    /** @var array<string,array{host:string,blog_id?:int,site?:string}> */
    private static $aliases = [];
    /** @var string|null requested alias host (lower-case, no port) */
    private static $alias = null;
    /** @var string internal host this alias stands in for */
    private static $internal = '';

    public static function map_file(): string {
        return WP_CONTENT_DIR . '/wpops-host-aliases.json';
    }

    /** @return array<string,array> */
    public static function load(): array {
        if (self::$aliases) {
            return self::$aliases;
        }
        $file = self::map_file();
        if (!is_readable($file)) {
            return [];
        }
        $data = json_decode((string) file_get_contents($file), true);
        if (!is_array($data) || empty($data['aliases']) || !is_array($data['aliases'])) {
            return [];
        }
        $out = [];
        foreach ($data['aliases'] as $host => $entry) {
            $host = strtolower(trim((string) $host));
            if ($host === '' || !is_array($entry) || empty($entry['host'])) {
                continue;
            }
            $out[$host] = [
                'host'    => strtolower((string) $entry['host']),
                'blog_id' => isset($entry['blog_id']) ? (int) $entry['blog_id'] : 0,
                'site'    => isset($entry['site']) ? (string) $entry['site'] : '',
            ];
        }
        self::$aliases = $out;
        return $out;
    }

    public static function request_host(): string {
        $h = isset($_SERVER['HTTP_HOST']) ? (string) $_SERVER['HTTP_HOST'] : '';
        $h = strtolower(preg_replace('/:\d+$/', '', $h));
        return $h;
    }

    /** Alias entry for the current request, or null. */
    public static function current(): ?array {
        $aliases = self::load();
        $h = self::request_host();
        if ($h === '' || !isset($aliases[$h])) {
            return null;
        }
        self::$alias = $h;
        self::$internal = $aliases[$h]['host'];
        return $aliases[$h];
    }

    public static function boot(): void {
        $entry = self::current();
        if ($entry === null) {
            return;
        }
        if (!defined('COOKIE_DOMAIN')) {
            define('COOKIE_DOMAIN', self::$alias);
        }
        if (!defined('KC_HOST_ALIAS_ACTIVE')) {
            define('KC_HOST_ALIAS_ACTIVE', true);
        }
        // Diagnostic: lets an operator (or wpops) tell a failover response apart
        // from the primary without guessing. Carries no secret.
        add_action('send_headers', static function () use ($entry) {
            if (!headers_sent()) {
                header('X-KC-Host-Alias: ' . (isset($entry['site']) && '' !== $entry['site'] ? $entry['site'] : 'active'));
            }
        }, 0);
        $alias = self::$alias;
        $url = 'https://' . $alias;
        $blog = 0;
        if (is_multisite() && isset($GLOBALS['current_blog']->blog_id)) {
            // sunrise.php picked the blog; keep its path (path-based networks).
            $blog = (int) $GLOBALS['current_blog']->blog_id;
            $url .= rtrim((string) $GLOBALS['current_blog']->path, '/');
        }
        $to_alias = static function ($value) use ($url, $blog) {
            if ($blog && get_current_blog_id() !== $blog) {
                // Another blog (switch_to_blog): only map the internal host.
                return is_string($value) ? self::replace_host($value) : $value;
            }
            return $url;
        };
        add_filter('pre_option_home', $to_alias, 1);
        add_filter('pre_option_siteurl', $to_alias, 1);
        add_filter('option_home', $to_alias, 1);
        add_filter('option_siteurl', $to_alias, 1);
        add_filter('network_home_url', [__CLASS__, 'rewrite_url'], 1);
        add_filter('network_site_url', [__CLASS__, 'rewrite_url'], 1);
        add_filter('content_url', [__CLASS__, 'rewrite_url'], 1);
        add_filter('plugins_url', [__CLASS__, 'rewrite_url'], 1);
        add_filter('upload_dir', [__CLASS__, 'rewrite_upload_dir'], 1);
        add_filter('wp_get_attachment_url', [__CLASS__, 'rewrite_url'], 1);
        add_filter('rest_url', [__CLASS__, 'rewrite_url'], 1);
        add_filter('allowed_redirect_hosts', [__CLASS__, 'allow_redirect'], 10, 1);
        // Never let canonical redirects bounce visitors back to the internal host.
        add_filter('redirect_canonical', [__CLASS__, 'guard_canonical'], 1, 2);
        // Hard-coded URLs inside stored content: rewrite the rendered response.
        add_action('template_redirect', [__CLASS__, 'start_buffer'], -1000);
        add_action('admin_init', [__CLASS__, 'start_buffer'], -1000);
        add_filter('rest_pre_echo_response', [__CLASS__, 'rewrite_rest'], 1000, 1);
        add_action('wp_head', [__CLASS__, 'noindex_hint'], 0);
    }

    public static function rewrite_url($url) {
        if (!is_string($url) || self::$alias === null || self::$internal === '') {
            return $url;
        }
        return self::replace_host($url);
    }

    public static function rewrite_upload_dir($dirs) {
        foreach (['url', 'baseurl'] as $k) {
            if (isset($dirs[$k]) && is_string($dirs[$k])) {
                $dirs[$k] = self::replace_host($dirs[$k]);
            }
        }
        return $dirs;
    }

    public static function allow_redirect($hosts) {
        $hosts = is_array($hosts) ? $hosts : [];
        if (self::$alias !== null) {
            $hosts[] = self::$alias;
        }
        return array_values(array_unique($hosts));
    }

    public static function guard_canonical($redirect, $requested) {
        if (is_string($redirect) && self::$internal !== '' && stripos($redirect, '//' . self::$internal) !== false) {
            return self::replace_host($redirect);
        }
        return $redirect;
    }

    public static function start_buffer(): void {
        static $started = false;
        if ($started || self::$alias === null) {
            return;
        }
        $started = true;
        ob_start([__CLASS__, 'rewrite_body']);
    }

    public static function rewrite_body($html) {
        if (!is_string($html) || $html === '' || self::$internal === '') {
            return $html;
        }
        return self::replace_host($html);
    }

    public static function rewrite_rest($result) {
        if (self::$internal === '') {
            return $result;
        }
        $json = wp_json_encode($result);
        if (!is_string($json)) {
            return $result;
        }
        $esc_internal = str_replace('/', '\/', self::$internal);
        $esc_alias = str_replace('/', '\/', self::$alias);
        $json = str_ireplace(['https:\/\/' . $esc_internal, 'http:\/\/' . $esc_internal], 'https:\/\/' . $esc_alias, $json);
        $json = self::replace_host($json);
        $decoded = json_decode($json, true);
        return $decoded === null ? $result : $decoded;
    }

    /** Replace every reference to the internal host with the alias (scheme-less and both schemes). */
    public static function replace_host(string $s): string {
        $i = preg_quote(self::$internal, '/');
        return (string) preg_replace('/(https?:)?\/\/' . $i . '(?![a-z0-9.-])/i', 'https://' . self::$alias, $s);
    }

    public static function noindex_hint(): void {
        // Nothing: the alias IS the production domain during failover; indexing must stay on.
    }
}

// Honour the proxy/tunnel scheme before anything builds URLs, but only when
// the request came from the private network (nginx / cloudflared containers),
// so a direct plain-HTTP client cannot claim HTTPS.
if (kc_host_alias_private_peer()) {
    if (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && strtolower((string) $_SERVER['HTTP_X_FORWARDED_PROTO']) === 'https') {
        $_SERVER['HTTPS'] = 'on';
    }
    if (isset($_SERVER['HTTP_CF_VISITOR']) && strpos((string) $_SERVER['HTTP_CF_VISITOR'], 'https') !== false) {
        $_SERVER['HTTPS'] = 'on';
    }
}

function kc_host_alias_private_peer(): bool {
    $ip = isset($_SERVER['REMOTE_ADDR']) ? (string) $_SERVER['REMOTE_ADDR'] : '';
    if ($ip === '' || !filter_var($ip, FILTER_VALIDATE_IP)) {
        return false;
    }
    // Private/loopback only (FILTER_FLAG_NO_PRIV_RANGE|NO_RES_RANGE rejects exactly those).
    return filter_var($ip, FILTER_VALIDATE_IP, FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE) === false;
}

KC_Host_Alias::boot();
