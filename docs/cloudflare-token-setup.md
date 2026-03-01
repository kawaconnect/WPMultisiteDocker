# Cloudflare API Token Setup

Each Cloudflare account in `.env` needs an API token (`CF_ACCOUNT_N_TOKEN`). This doc covers exactly what permissions to set and how to scope them.

## Required Permissions

Every script in this project uses the same token per account. The token needs exactly **3 permissions**:

| Permission | Access | Why |
|---|---|---|
| **Zone > Zone > Read** | Read | List zones (auto-discovery), look up zone IDs by domain name |
| **Zone > DNS > Edit** | Edit | Read DNS records (backups, status). Create/delete A and CNAME records (setup, failover, rollback) |
| **Account > Cloudflare Tunnel > Edit** | Edit | List/create tunnels, read tunnel configs (backups, status). Configure tunnel ingress (setup, failover, rollback) |

That's it. Nothing else is needed. No "Account Settings", no "SSL", no "Firewall".

> **Note:** "Edit" includes "Read" in Cloudflare's permission model. You cannot grant DNS Edit without DNS Read, it's implicit.

## Permission Mapping by Script

| Script | Zone:Zone | Zone:DNS | Account:Tunnel | Mutates? |
|---|---|---|---|---|
| `cf-status.sh` | Read | Read | Read | No |
| `cf-backup.sh` | Read | Read | Read | No |
| `cf-update-ips.sh` | - | - | - | No (fetches public IPs, no API token used) |
| `cf-setup.sh` | Read | Edit | Edit | Yes (standby domain DNS + tunnel creation) |
| `cf-failover.sh` | Read | Edit | Edit | Yes (switches A→CNAME on real domains) |
| `cf-rollback.sh` | Read | Edit | Edit | Yes (switches CNAME→A on real domains) |

### Every API endpoint called

```
GET  /user/tokens/verify                                    # (implicit, any valid token)
GET  /zones?name=...&status=active                          # Zone > Zone > Read
GET  /zones?account.id=...&status=active&per_page=50        # Zone > Zone > Read
GET  /zones/{zone_id}/dns_records?...                       # Zone > DNS > Read (via Edit)
POST /zones/{zone_id}/dns_records                           # Zone > DNS > Edit
DELETE /zones/{zone_id}/dns_records/{record_id}             # Zone > DNS > Edit
GET  /accounts/{id}/cfd_tunnel?...                          # Account > Cloudflare Tunnel > Read (via Edit)
POST /accounts/{id}/cfd_tunnel                              # Account > Cloudflare Tunnel > Edit
GET  /accounts/{id}/cfd_tunnel/{tunnel_id}/token            # Account > Cloudflare Tunnel > Read (via Edit)
GET  /accounts/{id}/cfd_tunnel/{tunnel_id}/configurations   # Account > Cloudflare Tunnel > Read (via Edit)
PUT  /accounts/{id}/cfd_tunnel/{tunnel_id}/configurations   # Account > Cloudflare Tunnel > Edit
```

## Step-by-Step: Create the Token

1. Go to https://dash.cloudflare.com/profile/api-tokens
2. Click **Create Token**
3. Click **Get started** next to "Create Custom Token"
4. Configure:

| Field | Value |
|---|---|
| **Token name** | `wp-docker-failover` (or any descriptive name) |
| **Permissions** | |
| Zone > Zone > Read | *(row 1)* |
| Zone > DNS > Edit | *(row 2)* |
| Account > Cloudflare Tunnel > Edit | *(row 3)* |
| **Zone Resources** | Include > All zones from > **Specific account** > *(select your account)* |
| **Account Resources** | Include > **Specific account** > *(select your account)* |
| **Client IP filtering** | *(optional)* Lock to your server IP for extra security |
| **TTL** | *(optional)* Set an expiry date if desired |

5. Click **Continue to summary**, then **Create Token**
6. Copy the token, you won't see it again
7. Put it in `.env`:
   ```
   CF_ACCOUNT_1_TOKEN=your-token-here
   ```
8. Verify it works:
   ```bash
   ./scripts/cf-status.sh
   ```

## Multi-Account Setup

Create a **separate token per Cloudflare account**. Each token is scoped to its own account's zones and tunnels.

```env
# Account 1: token scoped to account 1's zones and tunnels
CF_ACCOUNT_1_NAME=standby.example.net
CF_ACCOUNT_1_ID=abc123...
CF_ACCOUNT_1_TOKEN=token-for-account-1

# Account 2: token scoped to account 2's zones and tunnels
CF_ACCOUNT_2_NAME=client-account
CF_ACCOUNT_2_ID=def456...
CF_ACCOUNT_2_TOKEN=token-for-account-2
```

Never use a global API key. Always use scoped API tokens.

## Optional: Read-Only Token

If you want a separate read-only token (for monitoring/backups only), create a second token with:

| Permission | Access |
|---|---|
| Zone > Zone > Read | Read |
| Zone > DNS > Read | Read |
| Account > Cloudflare Tunnel > Read | Read |

This token can run `cf-status.sh` and `cf-backup.sh` but cannot run `cf-setup.sh`, `cf-failover.sh`, or `cf-rollback.sh`.
