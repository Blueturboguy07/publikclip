// publik_gate.rs — publikclip's glue around the publik module (src/publik.rs):
//   * the Tauri window that draws the gate's screens (Sign in with publik, Choose how to
//     pay for AI, Connect your plan) before the main window exists;
//   * the commands the main UI calls for the publik status and the pay choice;
//   * the one-time move of a 0.2.7 key out of ~/.publikclip/secrets.json.
// The module owns every call to publik; nothing here talks to the API on its own.
use std::fs;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, EventTarget, Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_opener::OpenerExt;

use crate::publik::{self, GateUi, Provider, Screen};

const GATE_LABEL: &str = "publik-gate";
const MAIN_LABEL: &str = "main";
/// The event the gate page listens for; its payload is `screen_json`.
const SCREEN_EVENT: &str = "publik-gate-screen";

/// What the gate page shows now, where its button presses go, and the last wallet the
/// module read (the main UI shows it; no key is ever in here).
#[derive(Default)]
pub struct GateState {
    screen: Mutex<Option<Value>>,
    presses: Mutex<Option<Sender<String>>>,
    wallet: Mutex<Option<Value>>,
}

fn screen_json(s: &Screen) -> Value {
    json!({
        "id": s.id,
        "title": s.title,
        "body": s.body,
        "note": s.note,
        "buttons": s.buttons.iter().map(|(id, label)| json!({ "id": id, "label": label })).collect::<Vec<_>>(),
    })
}

fn read_json(path: &Path) -> Value {
    fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .filter(|v| v.is_object())
        .unwrap_or_else(|| json!({}))
}

/// The site the API lives on (https://publikhq.com, or PUBLIK_ORIGIN in tests).
fn site() -> String {
    publik::default_base_url().trim_end_matches("/api/v1").to_string()
}

/// Build a window on the main thread and wait for it (window creation must not
/// happen on the gate's own thread).
fn build_on_main_thread(app: &AppHandle, build: impl FnOnce(&AppHandle) -> Result<(), String> + Send + 'static) {
    let (tx, rx) = channel::<Result<(), String>>();
    let handle = app.clone();
    if app.run_on_main_thread(move || { let _ = tx.send(build(&handle)); }).is_ok() {
        let _ = rx.recv_timeout(Duration::from_secs(30));
    }
}

fn ensure_gate_window(app: &AppHandle, title: &str) {
    if let Some(w) = app.get_webview_window(GATE_LABEL) {
        let _ = w.set_title(title);
        return;
    }
    let title = title.to_string();
    build_on_main_thread(app, move |handle| {
        WebviewWindowBuilder::new(handle, GATE_LABEL, WebviewUrl::App("gate.html".into()))
            .title(&title)
            .inner_size(480.0, 340.0)
            .resizable(false)
            .center()
            .build()
            .map(|_| ())
            .map_err(|e| e.to_string())
    });
}

fn open_main_window(app: &AppHandle) {
    if app.get_webview_window(MAIN_LABEL).is_some() {
        return;
    }
    build_on_main_thread(app, |handle| {
        WebviewWindowBuilder::new(handle, MAIN_LABEL, WebviewUrl::App("index.html".into()))
            .title("publikclip")
            .inner_size(1200.0, 800.0)
            .min_inner_size(900.0, 620.0)
            .build()
            .map(|_| ())
            .map_err(|e| e.to_string())
    });
}

/// The module's GateUi on a Tauri window: one small window, one screen at a time.
struct TauriGateUi {
    app: AppHandle,
}

impl GateUi for TauriGateUi {
    fn show(&mut self, screen: &Screen, done: Arc<AtomicBool>) -> Option<&'static str> {
        let (tx, rx) = channel::<String>();
        let payload = screen_json(screen);
        {
            let state = self.app.state::<GateState>();
            *state.screen.lock().unwrap() = Some(payload.clone());
            *state.presses.lock().unwrap() = Some(tx);
        }
        ensure_gate_window(&self.app, screen.title);
        let _ = self.app.emit_to(EventTarget::labeled(GATE_LABEL), SCREEN_EVENT, payload);
        loop {
            match rx.recv_timeout(Duration::from_millis(100)) {
                Ok(id) => {
                    if let Some((pressed, _)) = screen.buttons.iter().find(|(bid, _)| *bid == id) {
                        return Some(*pressed);
                    }
                }
                Err(RecvTimeoutError::Timeout) => {
                    if done.load(Ordering::SeqCst) {
                        return None;
                    }
                }
                Err(RecvTimeoutError::Disconnected) => return None,
            }
        }
    }

    fn open_url(&mut self, url: &str) {
        let _ = self.app.opener().open_url(url, None::<&str>);
    }
}

/// publikclip 0.2.7 and earlier kept the publik key in ~/.publikclip/secrets.json
/// (a "publik" block). Move it once to the publik credential file, which the module and
/// the pipeline both read; the old block goes, so there is one place for the key.
fn migrate_legacy_secret() {
    if publik::read_credential().is_some() {
        return;
    }
    let secrets_path = crate::home_dir().join("secrets.json");
    let mut secrets = read_json(&secrets_path);
    let block = secrets["publik"].clone();
    let Some(key) = block["key"].as_str().filter(|k| k.starts_with("pk_")) else { return };
    let credential = json!({
        "key": key,
        "base_url": block["base_url"].as_str().map(String::from).unwrap_or_else(publik::default_base_url),
        "install_id": block["install_id"],
        "claim_url": block["claim_url"],
        "choice": "publik",
    });
    let path = publik::credential_path();
    if let Some(dir) = path.parent() {
        if fs::create_dir_all(dir).is_err() {
            return;
        }
    }
    if fs::write(&path, serde_json::to_string_pretty(&credential).unwrap()).is_err() {
        return;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&path, fs::Permissions::from_mode(0o600));
    }
    if let Some(obj) = secrets.as_object_mut() {
        obj.remove("publik");
    }
    let _ = crate::write_secrets_at(&secrets_path, &secrets);
    let _ = fs::remove_file(crate::home_dir().join("publik-status.json"));
}

/// Called from the Tauri setup hook, before any window exists. The gate runs on its
/// own thread (it blocks until the person is signed in); then the main window opens
/// and the gate window closes, in that order, because Tauri exits with its last window.
pub fn start(app: &AppHandle) {
    let handle = app.clone();
    std::thread::spawn(move || {
        migrate_legacy_secret();
        let mut ui = TauriGateUi { app: handle.clone() };
        let (_key, wallet, _choice) = publik::ensure_signed_in(&mut ui);
        *handle.state::<GateState>().wallet.lock().unwrap() = Some(wallet);
        open_main_window(&handle);
        if let Some(gate) = handle.get_webview_window(GATE_LABEL) {
            let _ = gate.close();
        }
    });
}

// ---------- the gate page ----------

/// The gate page asks for the current screen when it loads (the event may have been
/// sent before the page listened).
#[tauri::command]
pub fn publik_gate_ready(state: tauri::State<'_, GateState>) -> Option<Value> {
    state.screen.lock().unwrap().clone()
}

/// A button on the gate page.
#[tauri::command]
pub fn publik_gate_press(id: String, state: tauri::State<'_, GateState>) {
    if let Some(tx) = state.presses.lock().unwrap().as_ref() {
        let _ = tx.send(id);
    }
}

// ---------- the main UI ----------

/// The person's own Gemini key, as resolve_provider() wants it (base URL, key).
fn own_gemini_key() -> Option<(String, String)> {
    crate::read_secrets()["gemini_api_key"]
        .as_str()
        .filter(|k| !k.trim().is_empty())
        .map(|k| ("https://generativelanguage.googleapis.com".to_string(), k.trim().to_string()))
}

/// Everything the UI shows about publik. Never the key.
fn status_json(wallet: Option<&Value>, refreshed: Option<bool>) -> Value {
    let credential = publik::read_credential();
    let choice = credential.as_ref().and_then(|c| c["choice"].as_str().map(String::from));
    let own_key = own_gemini_key();
    let provider = match publik::resolve_provider(own_key.clone()) {
        Provider::Publik { .. } => "publik",
        Provider::Own { .. } => "own",
        Provider::NeedsOwnKey => "needs_own_key",
    };
    let w = wallet.cloned().unwrap_or_else(|| json!({}));
    let wallet_url = format!("{}/wallet", site());
    json!({
        "signed_in": credential.is_some() && w["claim_state"] == "claimed",
        "handle": w["account"]["handle"],
        "choice": choice,
        "provider": provider,
        "balance_micros": w["balance_micros"],
        "plan": if w["plan"].is_object() { w["plan"].clone() } else { json!({ "kind": "none", "label": null }) },
        "top_up_url": w["top_up_url"].as_str().unwrap_or(&wallet_url),
        "wallet_url": wallet_url,
        "has_gemini_key": own_key.is_some(),
        // publik-status.json: the pipeline writes it after every publik API call (a 402
        // leaves needs_credit, message and top_up_url there).
        "last_call": read_json(&crate::home_dir().join("publik-status.json")),
        "refreshed": refreshed,
    })
}

/// The files on disk and the last wallet read; no network.
#[tauri::command]
pub fn publik_status(state: tauri::State<'_, GateState>) -> Result<Value, String> {
    let wallet = state.wallet.lock().unwrap().clone();
    Ok(status_json(wallet.as_ref(), None))
}

/// GET /wallet through the module. Offline, the last wallet stays and `refreshed` is false.
#[tauri::command]
pub async fn publik_refresh(state: tauri::State<'_, GateState>) -> Result<Value, String> {
    let key = publik::read_credential().and_then(|c| c["key"].as_str().map(String::from));
    let fresh = match key {
        Some(key) => tauri::async_runtime::spawn_blocking(move || publik::read_wallet(&key))
            .await
            .map_err(|e| e.to_string())?
            .ok()
            .and_then(|(_, w, _)| w),
        None => None,
    };
    if let Some(w) = &fresh {
        *state.wallet.lock().unwrap() = Some(w.clone());
        if publik::has_plan(w) {
            // money arrived since the last call: the pipeline's 402 note is stale
            let path = crate::home_dir().join("publik-status.json");
            let mut last = read_json(&path);
            if last["needs_credit"] == json!(true) {
                if let Some(obj) = last.as_object_mut() {
                    obj.remove("needs_credit");
                    obj.remove("message");
                }
                let _ = fs::write(&path, last.to_string());
            }
        }
    }
    let wallet = state.wallet.lock().unwrap().clone();
    Ok(status_json(wallet.as_ref(), Some(fresh.is_some())))
}

/// The setting the module asks for: "publik" (Connect your plan), "own_key" (Use my own
/// key), or null to ask again at the next start.
#[tauri::command]
pub fn publik_set_choice(choice: Option<String>, state: tauri::State<'_, GateState>) -> Result<Value, String> {
    let choice = choice.filter(|c| c == "publik" || c == "own_key");
    publik::set_choice(choice.as_deref());
    let wallet = state.wallet.lock().unwrap().clone();
    Ok(status_json(wallet.as_ref(), None))
}

/// The backstop behind the run button: a job starts only on a signed-in computer, and
/// scores on publik API only after Connect your plan (or with PUBLIK_API_KEY set).
pub(crate) fn require_provider(llm: Option<&str>) -> Result<(), String> {
    if publik::read_credential().is_none() && std::env::var("PUBLIK_API_KEY").is_err() {
        return Err("Sign in with publik to start.".into());
    }
    if llm == Some("publik") {
        if let Provider::Publik { .. } = publik::resolve_provider(None) {
            return Ok(());
        }
        return Err("Connect your publik plan in brain & keys, or score with your own Gemini key or Ollama.".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_never_carries_the_key() {
        let wallet = json!({ "claim_state": "claimed", "account": { "handle": "mann" }, "plan": { "kind": "plan", "label": "Plus" },
            "balance_micros": 120000, "top_up_url": "https://publikhq.com/wallet?top_up=1", "key": "pk_live_never" });
        let s = status_json(Some(&wallet), Some(true));
        assert_eq!(s["handle"], "mann");
        assert_eq!(s["plan"]["label"], "Plus");
        assert_eq!(s["balance_micros"], 120000);
        assert_eq!(s["refreshed"], true);
        assert!(!s.to_string().contains("pk_live_never"));
        assert!(s["provider"].is_string());
    }

    #[test]
    fn a_missing_plan_reads_as_none() {
        let s = status_json(Some(&json!({ "claim_state": "claimed" })), None);
        assert_eq!(s["plan"]["kind"], "none");
        assert!(s["top_up_url"].as_str().unwrap().ends_with("/wallet"));
    }

    #[test]
    fn screen_json_keeps_ids_and_labels() {
        let s = publik::gate_screen("sign_in", None, Some("note"), None);
        let v = screen_json(&s);
        assert_eq!(v["title"], "Sign in with publik");
        assert_eq!(v["note"], "note");
        assert_eq!(v["buttons"][0]["id"], "sign_in");
        assert_eq!(v["buttons"][0]["label"], "Sign in with publik");
    }
}
