// publik API provisioning for the desktop shell. One public app token names
// this app (never a user); onboarding trades it for an install-bound key
// through the system curl — the same precedent as check_ollama, and the only
// HTTP this shell does. The pipeline's own venv does not exist yet at
// onboarding time (it bootstraps on the first run), so this cannot be a
// sidecar call. The key lands in ~/.publikclip/secrets.json (0600) next to the
// own-key fields; the pipeline reads it from there.
use std::fs;
use std::io::Write;
use std::path::Path;
use std::process::Stdio;

use serde_json::{json, Value};

use crate::{home_dir, quiet_command};

const PUBLIK_API: &str = "https://publikhq.com/api/v1";
// Public by design: it only authorises minting one capped key attributed to
// publikclip, which is what the app itself does — and every user builds this
// app from source, so it has to live in the repo. Rotated by committing a new
// file; overridable at build time (PUBLIK_APP_TOKEN) for a CI-cut build.
const APP_TOKEN: &str = match option_env!("PUBLIK_APP_TOKEN") {
    Some(t) => t,
    None => include_str!("../publik-app-token.txt"),
};
const DISCLOSURE_VERSION: u32 = 1;

fn secrets_path() -> std::path::PathBuf {
    home_dir().join("secrets.json")
}

fn status_path() -> std::path::PathBuf {
    home_dir().join("publik-status.json")
}

fn read_json(path: &Path) -> Value {
    fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .filter(|v| v.is_object())
        .unwrap_or_else(|| json!({}))
}

pub(crate) fn read_secrets() -> Value {
    read_json(&secrets_path())
}

/// Write the whole secrets object and chmod 600 — the one write path every
/// key goes through (Gemini, Pexels, publik).
pub(crate) fn write_secrets_at(path: &Path, current: &Value) -> Result<(), String> {
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    fs::write(path, serde_json::to_string_pretty(current).unwrap()).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

/// Merge one top-level field into secrets.json, keeping every other key.
pub(crate) fn merge_secret(field: &str, value: Value) -> Result<(), String> {
    let mut current = read_secrets();
    current[field] = value;
    write_secrets_at(&secrets_path(), &current)
}

/// POST against the gateway via curl. The JSON body goes on stdin and a
/// bearer key rides a curl config read from stdin — never argv, which is
/// world-readable in `ps`. The status code arrives on the trailing line.
fn curl_json(url: &str, bearer: Option<&str>, body: &Value) -> Result<(u16, Value), String> {
    let mut cmd = quiet_command("curl");
    cmd.args([
        "-sS", "-m", "20", "-X", "POST", url,
        "-H", "content-type: application/json",
        "-H", "accept: application/json",
        "-w", "\n%{http_code}",
    ]);
    let stdin_payload = match bearer {
        // Only the empty-body revoke call carries a key; its body is not
        // secret, so the stdin slot goes to the header.
        Some(key) => {
            cmd.args(["-K", "-", "--data-binary", &body.to_string()]);
            format!("header = \"authorization: Bearer {key}\"\n")
        }
        None => {
            cmd.args(["--data-binary", "@-"]);
            body.to_string()
        }
    };
    let mut child = cmd
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("curl: {e}"))?;
    if let Some(mut stdin) = child.stdin.take() {
        stdin.write_all(stdin_payload.as_bytes()).map_err(|e| e.to_string())?;
    }
    let out = child.wait_with_output().map_err(|e| e.to_string())?;
    parse_curl_output(&String::from_utf8_lossy(&out.stdout))
}

fn parse_curl_output(text: &str) -> Result<(u16, Value), String> {
    let (payload, code) = text.rsplit_once('\n').unwrap_or(("", text));
    let code: u16 = code.trim().parse().unwrap_or(0);
    if code == 0 {
        return Err("publik API is unreachable right now — check your connection, or use your own key for now.".into());
    }
    Ok((code, serde_json::from_str(payload).unwrap_or_else(|_| json!({}))))
}

fn device_name() -> String {
    quiet_command("hostname")
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .filter(|s| !s.is_empty())
        .map(|s| s.chars().take(120).collect())
        .unwrap_or_else(|| "this computer".into())
}

fn os_slug() -> &'static str {
    if cfg!(target_os = "macos") {
        "macos"
    } else if cfg!(target_os = "windows") {
        "windows"
    } else {
        "linux"
    }
}

fn mint_body(install_id: &str) -> Value {
    json!({
        "app_token": APP_TOKEN.trim(),
        "app_slug": "publikclip",
        "app_version": env!("CARGO_PKG_VERSION"),
        "os": os_slug(),
        "device_name": device_name(),
        "install_id": install_id,
        "disclosure_version": DISCLOSURE_VERSION,
        "dialects": ["gemini"],
    })
}

fn gateway_message(res: &Value) -> Option<String> {
    res["error"]["message"].as_str().map(String::from)
}

/// The secrets.json `publik` block from a successful POST /installs. A 200
/// replay carries `key: null`; the key we already hold is kept then. Returns
/// None when there is no key either way (the caller re-mints once).
pub(crate) fn block_from_mint(res: &Value, prior: &Value, install_id: &str) -> Option<Value> {
    let key = res["key"]
        .as_str()
        .or_else(|| prior["key"].as_str())
        .filter(|k| !k.is_empty())?
        .to_string();
    let pick = |field: &str| -> Value {
        if res[field].is_null() { prior[field].clone() } else { res[field].clone() }
    };
    Some(json!({
        "key": key,
        "key_id": pick("key_id"),
        "base_url": res["base_url"].as_str().or_else(|| prior["base_url"].as_str()).unwrap_or(PUBLIK_API),
        "claim_url": pick("claim_url"),
        "install_id": res["install_id"].as_str().unwrap_or(install_id),
        "claim_state": res["claim_state"].as_str().or_else(|| res["wallet"]["claim_state"].as_str()).unwrap_or("anonymous"),
        "add_credit_url": res["wallet"]["add_credit_url"].clone(),
        "disclosure": pick("disclosure"),
        "disclosure_version": DISCLOSURE_VERSION,
        "app_version": env!("CARGO_PKG_VERSION"),
        "minted_at": now_rfc3339(),
    }))
}

/// Seed publik-status.json from the mint so the first balance line is honest
/// before the first scoring call (the pipeline rewrites it after every call).
pub(crate) fn status_from_mint(res: &Value) -> Value {
    let wallet = &res["wallet"];
    let balance = [&res["balance_micros"], &wallet["balance_micros"], &wallet["available_micros"], &res["starter_micros"]]
        .into_iter()
        .find(|v| v.is_number())
        .cloned()
        .unwrap_or(Value::Null);
    json!({
        "balance_micros": balance,
        "starter_micros": res["starter_micros"],
        // a replay (key: null) reports starter_micros 0 = "no NEW starter",
        // not "none left"; leave it to the first call's header then
        "starter_remaining_micros": if res["key"].is_null() { Value::Null } else { res["starter_micros"].clone() },
        "claim_state": res["claim_state"].as_str().or_else(|| wallet["claim_state"].as_str()).unwrap_or("anonymous"),
        "top_up_url": wallet["top_up_url"],
        "needs_credit": false,
        "disconnected": false,
    })
}

fn new_install_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

/// Mint (or re-fetch) this computer's key. Runs only after the user tapped
/// "Continue with publik API" — the frontend owns that consent moment.
#[tauri::command]
pub async fn publik_provision() -> Result<Value, String> {
    let token = APP_TOKEN.trim();
    if !token.starts_with("pat_") {
        return Err("This build has no publik API app token. Use your own key for now.".into());
    }
    let mut secrets = read_secrets();
    let prior = secrets["publik"].clone();
    let mut install_id = prior["install_id"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(String::from)
        .unwrap_or_else(new_install_id);
    let mut prior_for_merge = prior.clone();
    // At most two mints: a replay with no key on disk, or an install finished
    // by its owner (403 install_revoked), each re-mint once under a fresh
    // install_id; anything else is an answer.
    for attempt in 0..2 {
        let (code, res) = curl_json(&format!("{PUBLIK_API}/installs"), None, &mint_body(&install_id))?;
        match code {
            200 | 201 => {}
            403 if attempt == 0 && res["error"]["type"] == "install_revoked" => {
                install_id = new_install_id();
                prior_for_merge = json!({});
                continue;
            }
            429 => {
                return Err(gateway_message(&res).unwrap_or_else(|| {
                    "publik API is busy setting up new computers. Try again in a minute, or use your own key.".into()
                }))
            }
            _ => {
                return Err(format!(
                    "publik API couldn't set up this computer ({code}{}). Use your own key for now.",
                    gateway_message(&res).map(|m| format!(": {m}")).unwrap_or_default()
                ))
            }
        }
        let Some(block) = block_from_mint(&res, &prior_for_merge, &install_id) else {
            if attempt == 0 {
                install_id = new_install_id();
                prior_for_merge = json!({});
                continue;
            }
            return Err("publik API didn't return a key. Use your own key for now.".into());
        };
        secrets["publik"] = block;
        write_secrets_at(&secrets_path(), &secrets)?; // the key hits disk before anything else
        let _ = fs::write(status_path(), status_from_mint(&res).to_string());
        return publik_status();
    }
    Err("publik API couldn't set up this computer. Use your own key for now.".into())
}

/// Everything the UI shows — never the key itself.
#[tauri::command]
pub fn publik_status() -> Result<Value, String> {
    let secrets = read_secrets();
    let block = &secrets["publik"];
    let status = read_json(&status_path());
    let claim_state = status["claim_state"]
        .as_str()
        .or_else(|| block["claim_state"].as_str())
        .unwrap_or("anonymous");
    Ok(json!({
        "provisioned": block["key"].as_str().map(|k| !k.is_empty()).unwrap_or(false),
        "claim_state": claim_state,
        "claim_url": block["claim_url"],
        "add_credit_url": block["add_credit_url"],
        "disclosure": block["disclosure"],
        "status": status,
    }))
}

/// "Disconnect publik API": self-revoke, forget the key, keep install_id so a
/// reconnect re-keys the same install (no second starter).
#[tauri::command]
pub async fn publik_disconnect() -> Result<Value, String> {
    let mut secrets = read_secrets();
    let block = secrets["publik"].clone();
    if let Some(key) = block["key"].as_str().filter(|k| !k.is_empty()) {
        let base = block["base_url"].as_str().unwrap_or(PUBLIK_API).trim_end_matches('/');
        // Best effort: offline still disconnects locally.
        let _ = curl_json(&format!("{base}/installs/revoke"), Some(key), &json!({}));
    }
    secrets["publik"] = json!({"install_id": block["install_id"]});
    write_secrets_at(&secrets_path(), &secrets)?;
    let _ = fs::remove_file(status_path());
    publik_status()
}

/// RFC 3339 UTC from SystemTime — informational only, not worth a chrono dep.
fn now_rfc3339() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // Howard Hinnant's civil_from_days.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + if m <= 2 { 1 } else { 0 };
    format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z", rem / 3600, rem % 3600 / 60, rem % 60)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mint_fixture(key: Value) -> Value {
        json!({
            "install_id": "0f8e2c1a-1111-4222-8333-444455556666",
            "key": key,
            "key_id": "abc123def456",
            "base_url": "https://publikhq.com/api/v1",
            "claim_code": "HK7F-2QWD",
            "claim_url": "https://publikhq.com/claim/HK7F-2QWD",
            "claim_state": "anonymous",
            "starter_micros": 250000,
            "balance_micros": 250000,
            "wallet": {"balance_micros": 250000, "add_credit_url": "https://publikhq.com/dashboard/api/add",
                       "top_up_url": "https://publikhq.com/claim/HK7F-2QWD"},
            "disclosure": {"version": 1, "cost": "The cost sentence.", "data_path": "The data sentence."}
        })
    }

    #[test]
    fn app_token_is_embedded() {
        assert!(APP_TOKEN.trim().starts_with("pat_publikclip_"));
    }

    #[test]
    fn block_from_fresh_mint() {
        let b = block_from_mint(&mint_fixture(json!("pk_live_x")), &json!(null), "local-id").unwrap();
        assert_eq!(b["key"], "pk_live_x");
        assert_eq!(b["key_id"], "abc123def456");
        assert_eq!(b["base_url"], "https://publikhq.com/api/v1");
        assert_eq!(b["claim_url"], "https://publikhq.com/claim/HK7F-2QWD");
        assert_eq!(b["install_id"], "0f8e2c1a-1111-4222-8333-444455556666");
        assert_eq!(b["disclosure"]["cost"], "The cost sentence.");
    }

    #[test]
    fn replay_keeps_the_key_on_disk_or_asks_for_a_remint() {
        let prior = json!({"key": "pk_live_old", "install_id": "i"});
        let b = block_from_mint(&mint_fixture(Value::Null), &prior, "i").unwrap();
        assert_eq!(b["key"], "pk_live_old");
        assert!(block_from_mint(&mint_fixture(Value::Null), &json!({"install_id": "i"}), "i").is_none());
    }

    #[test]
    fn status_seed_has_the_starter_balance() {
        let s = status_from_mint(&mint_fixture(json!("pk_live_x")));
        assert_eq!(s["balance_micros"], 250000);
        assert_eq!(s["claim_state"], "anonymous");
        assert_eq!(s["needs_credit"], false);
    }

    #[test]
    fn curl_output_parsing() {
        let (code, v) = parse_curl_output("{\"key\":\"k\"}\n201").unwrap();
        assert_eq!(code, 201);
        assert_eq!(v["key"], "k");
        assert!(parse_curl_output("\n000").is_err());
        assert!(parse_curl_output("").is_err());
    }

    #[test]
    fn mint_body_shape() {
        let b = mint_body("iid");
        assert_eq!(b["app_slug"], "publikclip");
        assert_eq!(b["disclosure_version"], 1);
        assert_eq!(b["dialects"], json!(["gemini"]));
        assert_eq!(b["app_version"], env!("CARGO_PKG_VERSION"));
        assert!(["macos", "windows", "linux"].contains(&b["os"].as_str().unwrap()));
    }

    #[test]
    fn write_secrets_keeps_other_keys_and_is_0600() {
        let dir = std::env::temp_dir().join(format!("publikclip-test-{}", new_install_id()));
        let path = dir.join("secrets.json");
        write_secrets_at(&path, &json!({"gemini_api_key": "AIza", "pexels_api_key": "px"})).unwrap();
        let mut current = read_json(&path);
        current["publik"] = json!({"key": "pk_live_x"});
        write_secrets_at(&path, &current).unwrap();
        let back = read_json(&path);
        assert_eq!(back["gemini_api_key"], "AIza");
        assert_eq!(back["pexels_api_key"], "px");
        assert_eq!(back["publik"]["key"], "pk_live_x");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        }
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn rfc3339_shape() {
        let s = now_rfc3339();
        assert_eq!(s.len(), 20);
        assert!(s.ends_with('Z') && s.as_bytes()[10] == b'T');
    }
}
