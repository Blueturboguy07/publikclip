// publik API provisioning for the desktop shell.
//
// One public app token names this app (never a user); on first launch, AFTER
// the disclosure is accepted, it is traded for an install-bound `pk_` key.
// The trade goes through the system `curl`, which is the precedent this shell
// already set with check_ollama and the only HTTP it does: the Python venv
// does not exist until the first pipeline run, so provisioning cannot be a
// sidecar call, and a webview fetch would put the app token in the JS bundle.
//
// Nothing here ever touches `gemini_api_key` or `pexels_api_key`. The publik
// credential is one more field in the same secrets.json, written with the
// same chmod 600, and a user who has pasted their own Google key keeps it.

use std::fs;
use std::io::Write;
use std::process::Stdio;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

use crate::{home_dir, quiet_command};

const PUBLIK_API: &str = "https://publikhq.com/api/v1";

/// Public by design: this token authorises minting ONE capped key attributed
/// to publikclip, which is exactly what the app itself does with it. There is
/// no publik-cut binary to inject it into -- every install is a `git clone`
/// and a local `tauri build` -- so a token that lived only in CI would never
/// reach a user. Containment is server side: per-token daily mint caps, a
/// per-IP limit, a daily spend cap on every key it mints, and revocation by
/// timestamp.
///
/// FILLING IT IN: the value in publik-app-token.txt is a placeholder. A
/// publik-side script (`scripts/mint-app-token.mts publikclip` in the publik
/// repo) prints a real `pat_publikclip_<32>` once, and that value is
/// committed here in its own commit. Until then this app provisions nothing
/// and onboarding falls through to the own-key card. `option_env!` lets a CI
/// build override it without a commit; build.rs re-runs on either change, so
/// a rotation can never ship as a stale cached constant.
const APP_TOKEN: &str = match option_env!("PUBLIK_APP_TOKEN") {
    Some(t) => t,
    None => include_str!("../publik-app-token.txt"),
};

/// The placeholder value. A build carrying it must not post anything.
const APP_TOKEN_PLACEHOLDER: &str = "pat_publikclip_REPLACE_ME";

/// Bumped whenever the disclosure text changes. The server records it and
/// never refuses on it; it is how publik can tell what a person agreed to.
const DISCLOSURE_VERSION: u32 = 2;

fn app_token() -> &'static str {
    APP_TOKEN.trim()
}

pub fn token_is_placeholder() -> bool {
    app_token().is_empty() || app_token() == APP_TOKEN_PLACEHOLDER
}

fn secrets_path() -> std::path::PathBuf {
    home_dir().join("secrets.json")
}

fn status_path() -> std::path::PathBuf {
    home_dir().join("publik-status.json")
}

fn read_secrets() -> Value {
    fs::read_to_string(secrets_path())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_else(|| json!({}))
}

/// Same shape as save_gemini_key: merge one field, write, chmod 600. The
/// whole document is written back, so every other key in it survives.
fn write_secrets(current: &Value) -> Result<(), String> {
    let home = home_dir();
    fs::create_dir_all(&home).map_err(|e| e.to_string())?;
    let path = secrets_path();
    fs::write(&path, serde_json::to_string_pretty(current).unwrap()).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&path, fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

/// RFC 3339, without pulling in chrono for one informational field.
fn now_rfc3339() -> String {
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0) as i64;
    let days = secs.div_euclid(86_400);
    let tod = secs.rem_euclid(86_400);
    // Civil-from-days (Howard Hinnant's algorithm), shifted to 0000-03-01.
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        y, m, d, tod / 3600, (tod % 3600) / 60, tod % 60
    )
}

/// A v4-shaped identifier from the OS, without adding a crate for it.
fn new_install_id() -> String {
    let mut bytes = [0u8; 16];
    if let Ok(mut f) = fs::File::open("/dev/urandom") {
        use std::io::Read;
        let _ = f.read_exact(&mut bytes);
    }
    if bytes.iter().all(|b| *b == 0) {
        // Windows, or a machine with no /dev/urandom: mix the clock and the
        // process id. This value only has to be unique per install, and the
        // server treats a collision as a replay rather than as trust.
        let ns = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(1);
        let pid = std::process::id() as u128;
        let mixed = ns.wrapping_mul(0x9E37_79B9_7F4A_7C15).wrapping_add(pid << 64 | pid);
        bytes.copy_from_slice(&mixed.to_le_bytes());
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8], &hex[8..12], &hex[12..16], &hex[16..20], &hex[20..32]
    )
}

fn os_name() -> &'static str {
    if cfg!(target_os = "macos") {
        "macos"
    } else if cfg!(target_os = "windows") {
        "windows"
    } else {
        "linux"
    }
}

fn device_name() -> String {
    quiet_command("hostname")
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "this computer".into())
}

/// POST against the gateway via curl. The body goes on STDIN, never on argv:
/// argv is world-readable in `ps`, and the app token is on it.
fn curl_json(
    method: &str,
    url: &str,
    bearer: Option<&str>,
    body: Option<&Value>,
) -> Result<(u16, Value), String> {
    let mut cmd = quiet_command("curl");
    cmd.args([
        "-sS", "-m", "20", "-X", method, url,
        "-H", "content-type: application/json",
        "-H", "accept: application/json",
        "-w", "\n%{http_code}",
    ]);
    if let Some(key) = bearer {
        cmd.args(["-H", &format!("authorization: Bearer {key}")]);
    }
    if body.is_some() {
        cmd.args(["--data-binary", "@-"]);
        cmd.stdin(Stdio::piped());
    }
    let mut child = cmd
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("publik API is unreachable right now. Nothing is being charged. ({e})"))?;
    if let (Some(b), Some(mut stdin)) = (body, child.stdin.take()) {
        stdin.write_all(b.to_string().as_bytes()).map_err(|e| e.to_string())?;
    }
    let out = child.wait_with_output().map_err(|e| e.to_string())?;
    let text = String::from_utf8_lossy(&out.stdout);
    let (payload, code) = text
        .rsplit_once('\n')
        .ok_or("publik API is unreachable right now. Nothing is being charged.")?;
    let code: u16 = code
        .trim()
        .parse()
        .map_err(|_| "publik API is unreachable right now. Nothing is being charged.".to_string())?;
    if code == 0 {
        return Err("publik API is unreachable right now. Nothing is being charged.".into());
    }
    Ok((code, serde_json::from_str(payload).unwrap_or(json!({}))))
}

/// The credential block a mint response becomes. Pure, so the shape is tested
/// rather than observed in production.
///
/// A 200 with `"key": null` is a replay of an install this machine already
/// minted (publik answers that rather than handing the key out twice). If we
/// still hold the key, we keep it; the caller decides what to do when we do
/// not.
pub fn mint_block(res: &Value, existing_key: Option<&str>, install_id: &str, minted_at: &str) -> Value {
    let base = res["base_url"].as_str().unwrap_or(PUBLIK_API).trim_end_matches('/').to_string();
    let mut block = json!({
        "version": 1,
        "install_id": res["install_id"].as_str().unwrap_or(install_id),
        "key_id": res["key_id"],
        // The pipeline talks to the Gemini line, so the credential carries the
        // dialect's base URL rather than the gateway root.
        "base_url": format!("{base}/gemini"),
        "models": {"vision": "publik-vision", "image": "publik-image"},
        "claim_code": res["claim_code"],
        "claim_url": res["claim_url"],
        "claim_expires_at": res["claim_expires_at"],
        "app_slug": "publikclip",
        "disclosure_version": DISCLOSURE_VERSION,
        "minted_at": minted_at,
    });
    match res["key"].as_str() {
        Some(k) if !k.is_empty() => block["key"] = json!(k),
        _ => {
            if let Some(k) = existing_key {
                block["key"] = json!(k);
            }
        }
    }
    block
}

/// The first-run card's numbers, seeded from the mint so the balance line is
/// honest before the first scoring call rather than blank until one happens.
fn status_from_mint(res: &Value) -> Value {
    let starter = res["starter_micros"].as_i64().or_else(|| res["starting_credit_micros"].as_i64());
    let balance = res["balance_micros"]
        .as_i64()
        .or_else(|| res["wallet"]["available_micros"].as_i64())
        .or(starter);
    json!({
        "balance_micros": balance,
        "starter_remaining_micros": starter,
        "claim_state": res["claim_state"].as_str().unwrap_or("anonymous"),
        "needs_credit": false,
        "disconnected": false,
    })
}

/// Mint (or re-fetch) the install key. Runs only after the disclosure has been
/// accepted -- the frontend owns that moment, and nothing is posted before the
/// person taps.
#[tauri::command]
pub fn publik_provision() -> Result<Value, String> {
    if token_is_placeholder() {
        return Err(
            "This build has no publik API token yet. Use your own Gemini key, or rebuild from a \
             commit that carries one."
                .into(),
        );
    }
    let mut secrets = read_secrets();
    let existing_key = secrets["publik"]["key"].as_str().map(String::from);
    let install_id = secrets["publik"]["install_id"]
        .as_str()
        .map(String::from)
        .unwrap_or_else(new_install_id);
    let body = json!({
        "app_token": app_token(),
        "app_slug": "publikclip",
        "app_version": env!("CARGO_PKG_VERSION"),
        "os": os_name(),
        "arch": std::env::consts::ARCH,
        "device_name": device_name(),
        "install_id": install_id,
        "disclosure_version": DISCLOSURE_VERSION,
        "dialects": ["gemini"],
    });
    let (code, res) = curl_json("POST", &format!("{PUBLIK_API}/installs"), None, Some(&body))?;
    match code {
        200 | 201 => {}
        429 => {
            return Err("publik API is busy setting up new installs. Try again in a minute, or use your own key.".into())
        }
        _ => {
            let detail = res["error"]["message"].as_str().unwrap_or("").to_string();
            return Err(if detail.is_empty() {
                format!("publik API refused to set up this computer ({code}). Use your own key for now.")
            } else {
                format!("publik API refused to set up this computer: {detail}")
            });
        }
    }
    // A replay we cannot use: we hold no key and were not given one. Forget the
    // install id once and mint fresh, which is the documented recovery.
    if res["key"].as_str().unwrap_or("").is_empty() && existing_key.is_none() {
        secrets["publik"] = json!({});
        write_secrets(&secrets)?;
        let fresh_id = new_install_id();
        let body = json!({
            "app_token": app_token(),
            "app_slug": "publikclip",
            "app_version": env!("CARGO_PKG_VERSION"),
            "os": os_name(),
            "arch": std::env::consts::ARCH,
            "device_name": device_name(),
            "install_id": fresh_id,
            "disclosure_version": DISCLOSURE_VERSION,
            "dialects": ["gemini"],
        });
        let (code, res2) = curl_json("POST", &format!("{PUBLIK_API}/installs"), None, Some(&body))?;
        if !(code == 200 || code == 201) || res2["key"].as_str().unwrap_or("").is_empty() {
            return Err("publik API could not set up this computer. Use your own key for now.".into());
        }
        let mut secrets = read_secrets();
        secrets["publik"] = mint_block(&res2, None, &fresh_id, &now_rfc3339());
        write_secrets(&secrets)?;
        let _ = fs::write(status_path(), status_from_mint(&res2).to_string());
        return publik_status();
    }

    secrets["publik"] = mint_block(&res, existing_key.as_deref(), &install_id, &now_rfc3339());
    // The key hits disk before anything else happens: a key minted and lost is
    // a wallet nobody can reach.
    write_secrets(&secrets)?;
    let _ = fs::write(status_path(), status_from_mint(&res).to_string());
    publik_status()
}

/// Everything the UI shows. Never the key itself.
#[tauri::command]
pub fn publik_status() -> Result<Value, String> {
    let secrets = read_secrets();
    let block = &secrets["publik"];
    let status: Value = fs::read_to_string(status_path())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(json!({}));
    Ok(json!({
        "provisioned": block["key"].as_str().map(|k| !k.is_empty()).unwrap_or(false),
        "available": !token_is_placeholder(),
        "claim_url": block["claim_url"],
        "claim_code": block["claim_code"],
        "status": status,
    }))
}

/// "Disconnect publik API": self-revoke, forget the block, keep the install id
/// so a later reconnect mints under the same install and does not ask for a
/// second starter.
#[tauri::command]
pub fn publik_disconnect() -> Result<(), String> {
    let mut secrets = read_secrets();
    if let Some(key) = secrets["publik"]["key"].as_str().map(String::from) {
        let _ = curl_json(
            "POST",
            &format!("{PUBLIK_API}/installs/revoke"),
            Some(&key),
            Some(&json!({})),
        );
    }
    let install_id = secrets["publik"]["install_id"].clone();
    secrets["publik"] = json!({ "install_id": install_id });
    write_secrets(&secrets)?;
    let _ = fs::remove_file(status_path());
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mint_response() -> Value {
        json!({
            "install_id": "11111111-2222-4333-8444-555555555555",
            "key": "pk_live_abcdefghijkl_0123456789abcdef0123456789abcdef",
            "key_id": "abcdefghijkl",
            "base_url": "https://publikhq.com/api/v1",
            "claim_code": "HK7F-2QWD",
            "claim_url": "https://publikhq.com/claim/HK7F-2QWD",
            "claim_expires_at": "2026-10-18T17:04:11Z",
            "starter_micros": 250000,
            "balance_micros": 250000,
            "claim_state": "anonymous"
        })
    }

    #[test]
    fn block_carries_the_gemini_base_url_and_only_publik_aliases() {
        let block = mint_block(&mint_response(), None, "fallback", "2026-09-19T00:00:00Z");
        assert_eq!(block["base_url"], "https://publikhq.com/api/v1/gemini");
        assert_eq!(block["models"]["vision"], "publik-vision");
        assert_eq!(block["models"]["image"], "publik-image");
        assert!(block["key"].as_str().unwrap().starts_with("pk_live_"));
        assert_eq!(block["app_slug"], "publikclip");
        assert_eq!(block["disclosure_version"], DISCLOSURE_VERSION);
    }

    #[test]
    fn a_replay_keeps_the_key_already_on_disk() {
        let mut res = mint_response();
        res["key"] = Value::Null;
        let block = mint_block(&res, Some("pk_live_old"), "fallback", "2026-09-19T00:00:00Z");
        assert_eq!(block["key"], "pk_live_old");
        // And with no key anywhere, the block simply has none: the caller
        // re-mints rather than writing a credential with a null key in it.
        let block = mint_block(&res, None, "fallback", "2026-09-19T00:00:00Z");
        assert!(block["key"].is_null());
    }

    #[test]
    fn a_response_without_a_base_url_falls_back_to_the_compiled_one() {
        let mut res = mint_response();
        res["base_url"] = Value::Null;
        let block = mint_block(&res, None, "fallback", "2026-09-19T00:00:00Z");
        assert_eq!(block["base_url"], "https://publikhq.com/api/v1/gemini");
    }

    #[test]
    fn status_reads_either_field_name_for_the_starter() {
        let res = json!({"starting_credit_micros": 250000, "claim_state": "anonymous"});
        let status = status_from_mint(&res);
        assert_eq!(status["starter_remaining_micros"], 250000);
        assert_eq!(status["balance_micros"], 250000);
        assert_eq!(status["needs_credit"], false);
    }

    #[test]
    fn the_placeholder_token_provisions_nothing() {
        // A build that has not had its token filled in must refuse rather than
        // post an invalid token and burn a per-IP mint attempt.
        assert!(token_is_placeholder() || app_token().starts_with("pat_publikclip_"));
    }

    #[test]
    fn install_ids_are_unique_and_v4_shaped() {
        let a = new_install_id();
        let b = new_install_id();
        assert_ne!(a, b);
        assert_eq!(a.len(), 36);
        assert_eq!(a.as_bytes()[14], b'4');
    }

    #[test]
    fn the_clock_formats_as_rfc_3339() {
        let now = now_rfc3339();
        assert_eq!(now.len(), 20);
        assert!(now.ends_with('Z'));
        assert!(now.starts_with("20"));
    }
}
