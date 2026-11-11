// publik.rs — Sign in with publik, usage counting, and publik API as one provider option.
// At app start, before any window of your own: `let gate = publik::ensure_signed_in(&mut ui);`. It
//   1. loads or provisions this computer's publik key (POST /api/v1/installs);
//   2. shows Sign in with publik until GET /api/v1/wallet says "claimed" (no other way past it);
//   3. when USES_AI: asks once how the app pays for AI and stores the answer (set_choice() changes it);
//   4. starts usage counting: app_opened now, active_session every 30 minutes (POST /api/v1/telemetry).
//
// This is the publik module from https://publikhq.com/sdk/publik.rs.md. publikclip's own glue
// (the Tauri gate window, the status commands) is in publik_gate.rs. The pipeline reads the
// credential file this module writes (publikclip_pipeline/config.py publik_shared_file()).
// The parts of the module publikclip does not call (TerminalUi, search) stay, so the file is the
// module as published; they are allowed to be unused.
#![allow(dead_code)]
use serde_json::{json, Value};
use std::io::{BufRead, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

pub const APP_SLUG: &str = "publikclip";
pub const APP_VERSION: &str = env!("CARGO_PKG_VERSION");
/// false: the app calls no model. It still signs in and counts users; it skips the plan screen.
pub const USES_AI: bool = true;
/// Ask for a tier, never a vendor model name.
pub const MODELS: [&str; 3] = ["publik-fast", "publik-balanced", "publik-smart"];
// Public by design: it can only mint capped keys attributed to this app, and
// every user who builds from source needs it, so it lives in the repo
// (publik-app-token.txt next to Cargo.toml, tracked; build.rs watches it).
const APP_TOKEN: &str = match option_env!("PUBLIK_APP_TOKEN") {
    Some(t) => t,
    None => include_str!("../publik-app-token.txt"),
};
const OS: &str = if cfg!(target_os = "macos") { "macos" } else if cfg!(windows) { "windows" } else { "linux" };

fn origin() -> String { std::env::var("PUBLIK_ORIGIN").unwrap_or_else(|_| "https://publikhq.com".into()) } // PUBLIK_ORIGIN: tests only
pub fn default_base_url() -> String { format!("{}/api/v1", origin()) }

pub enum Provider { Own { base_url: String, api_key: String }, Publik { base_url: String, api_key: String }, NeedsOwnKey }
pub struct Screen { pub id: &'static str, pub title: &'static str, pub body: String, pub note: Option<&'static str>, pub buttons: Vec<(&'static str, &'static str)> }
/// show(): draw the screen and return the button id pressed. Once `done` is set (the gate saw the sign-in), close it and return None.
pub trait GateUi { fn show(&mut self, screen: &Screen, done: Arc<AtomicBool>) -> Option<&'static str>; fn open_url(&mut self, url: &str); }

/// ~/Library/Application Support/publik, %LOCALAPPDATA%\publik, ~/.config/publik: shared by every publik app.
fn publik_dir() -> PathBuf {
    let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")).map(PathBuf::from).unwrap_or_default();
    let base = if cfg!(target_os = "macos") { home.join("Library/Application Support") }
        else if cfg!(windows) { std::env::var_os("LOCALAPPDATA").map(PathBuf::from).unwrap_or(home.join("AppData/Local")) }
        else { std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from).unwrap_or(home.join(".config")) };
    base.join("publik")
}
pub fn credential_path() -> PathBuf { publik_dir().join("apps").join(format!("{APP_SLUG}.json")) }

pub fn read_credential() -> Option<Value> {
    let v: Value = serde_json::from_str(&std::fs::read_to_string(credential_path()).ok()?).ok()?;
    v["key"].as_str().filter(|k| k.starts_with("pk_"))?;
    Some(v)
}
fn write_credential(v: &Value) -> Result<(), String> {
    let path = credential_path();
    std::fs::create_dir_all(path.parent().unwrap()).map_err(|e| e.to_string())?;
    std::fs::write(&path, serde_json::to_string_pretty(v).unwrap()).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    { use std::os::unix::fs::PermissionsExt; let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)); }
    Ok(())
}
/// The person's answer to the plan screen: Some("publik"), Some("own_key"), or None to ask again. Settings call it.
pub fn set_choice(choice: Option<&str>) { if let Some(mut c) = read_credential() { c["choice"] = json!(choice); let _ = write_credential(&c); } }

/// The key this app uses: the stored choice decides. PUBLIK_API_KEY / PUBLIK_API_BASE_URL override (development).
pub fn resolve_provider(user_key: Option<(String, String)>) -> Provider {
    if let Ok(key) = std::env::var("PUBLIK_API_KEY") {
        return Provider::Publik { base_url: std::env::var("PUBLIK_API_BASE_URL").unwrap_or_else(|_| default_base_url()), api_key: key };
    }
    if let Some(c) = read_credential().filter(|c| c["choice"] == "publik") {
        return Provider::Publik { base_url: c["base_url"].as_str().map(String::from).unwrap_or_else(default_base_url), api_key: c["key"].as_str().unwrap_or_default().into() };
    }
    match user_key.filter(|(_, k)| !k.is_empty()) { Some((base_url, api_key)) => Provider::Own { base_url, api_key }, None => Provider::NeedsOwnKey }
}

/// One curl call; `stdin` carries the body or a config with the key. Err: publik is not reachable.
/// publikclip: through quiet_command, so no console window flashes on Windows.
fn curl(args: &[&str], stdin: &str) -> Result<(u16, Value), String> {
    let mut child = crate::quiet_command("curl").args(["-sS", "-w", "\n%{http_code}"]).args(args)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().map_err(|e| format!("curl: {e}"))?;
    child.stdin.take().unwrap().write_all(stdin.as_bytes()).map_err(|e| e.to_string())?;
    let out = String::from_utf8_lossy(&child.wait_with_output().map_err(|e| e.to_string())?.stdout).to_string();
    let (payload, code) = out.rsplit_once('\n').unwrap_or(("", &out));
    match code.trim().parse::<u16>().unwrap_or(0) {
        0 => Err("publik is not reachable. Check your connection.".into()),
        code => Ok((code, serde_json::from_str(payload).unwrap_or(json!({})))),
    }
}
fn key_config(key: &str) -> String { format!("header = \"Authorization: Bearer {key}\"\n") }

/// POST /api/v1/installs: this computer's publik key. Err is a readable message.
pub fn provision() -> Result<Value, String> {
    let runtime = std::env::var("PUBLIK_APP_TOKEN").unwrap_or_default();
    let token = if runtime.trim().is_empty() { APP_TOKEN.trim() } else { runtime.trim() };
    if !token.starts_with("pat_") { return Err("This build has no publik app token. Ask the developer for a new build.".into()); }
    let mut prior = read_credential().unwrap_or(json!({}));
    let mut install_id = prior["install_id"].as_str().map(String::from).unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    for _ in 0..2 {
        let body = json!({ "app_token": token, "app_slug": APP_SLUG, "app_version": APP_VERSION, "os": OS, "arch": std::env::consts::ARCH,
            "install_id": install_id, "disclosure_version": 1, "dialects": ["chat_completions"] });
        let url = format!("{}/api/v1/installs", origin());
        let (code, res) = curl(&["-m", "20", "-X", "POST", &url, "-H", "content-type: application/json", "--data-binary", "@-"], &body.to_string())?;
        let key = res["key"].as_str().or(if code == 200 { prior["key"].as_str() } else { None }).map(String::from);
        if let (200 | 201, Some(key)) = (code, key) {
            let c = json!({ "key": key, "base_url": res["base_url"].as_str().map(String::from).unwrap_or_else(default_base_url),
                "install_id": res["install_id"].as_str().unwrap_or(&install_id), "claim_url": res["claim_url"], "choice": prior["choice"] });
            write_credential(&c)?; // the key reaches disk before anything else sees it
            return Ok(c);
        }
        if code == 200 || res["error"]["type"] == "install_revoked" { install_id = uuid::Uuid::new_v4().to_string(); prior = json!({}); continue; }
        return Err(res["error"]["message"].as_str().map(String::from).unwrap_or_else(|| format!("publik could not set up this computer ({code}).")));
    }
    Err("publik could not set up this computer.".into())
}

/// GET /api/v1/wallet: (status, wallet or None, message). Err: publik is not reachable.
pub fn read_wallet(key: &str) -> Result<(u16, Option<Value>, Option<String>), String> {
    let url = format!("{}/api/v1/wallet", origin());
    let (code, mut b) = curl(&["-m", "15", "-K", "-", &url], &key_config(key))?;
    if code != 200 || !b["claim_state"].is_string() { return Ok((code, None, b["error"]["message"].as_str().map(String::from))); }
    if !b["plan"].is_object() { b["plan"] = json!({ "kind": "none", "label": null }); }
    Ok((code, Some(b), None))
}

/// The words of the gate. The same in every publik module.
pub fn gate_screen(id: &'static str, w: Option<&Value>, note: Option<&'static str>, message: Option<String>) -> Screen {
    if id == "unreachable" {
        return Screen { id, title: "Sign in with publik", body: message.unwrap_or_else(|| "publik is not reachable. Check your connection.".into()), note, buttons: vec![("retry", "Retry")] };
    }
    if id == "sign_in" {
        return Screen { id, title: "Sign in with publik", note, buttons: vec![("sign_in", "Sign in with publik")],
            body: "This app uses your publik account. Click Sign in with publik, then sign in in your browser. This window continues when you are signed in.".into() };
    }
    let w = w.cloned().unwrap_or(json!({ "plan": { "kind": "none" } }));
    let who = match w["account"]["handle"].as_str() { Some(h) => format!("You are signed in as @{h}."), None => "You are signed in to publik.".into() };
    if !has_plan(&w) {
        return Screen { id, title: "Choose how to pay for AI", note, body: format!("{who} This app uses AI. Get a publik plan, or use your own key."),
            buttons: vec![("get_plan", "Get a plan"), ("own_key", "Use my own key")] };
    }
    let what = if w["plan"]["kind"] == "plan" { format!("Your plan: {}.", w["plan"]["label"].as_str().unwrap_or("publik")) }
        else { format!("Your balance: ${:.2}.", w["balance_micros"].as_i64().unwrap_or(0) as f64 / 1_000_000.0) };
    Screen { id, title: "Connect your plan", note, body: format!("{who} {what} Connect it to use AI in this app, or use your own key."),
        buttons: vec![("connect", "Connect your plan"), ("own_key", "Use my own key")] }
}
fn claimed(w: &Value) -> bool { w["claim_state"] == "claimed" }
pub fn has_plan(w: &Value) -> bool { w["plan"]["kind"].as_str().unwrap_or("none") != "none" || w["balance_micros"].as_i64().unwrap_or(0) > 0 }

/// Show a screen; with `until`, poll GET /wallet every 3 s for at most 10 minutes. "done": the poll saw it; "timeout": it did not.
fn ask(ui: &mut dyn GateUi, screen: &Screen, key: &str, until: Option<fn(&Value) -> bool>) -> &'static str {
    let done = Arc::new(AtomicBool::new(false));
    let polled: Arc<Mutex<Option<&'static str>>> = Arc::new(Mutex::new(None));
    if let Some(until) = until {
        let (done, polled, key) = (done.clone(), polled.clone(), key.to_string());
        std::thread::spawn(move || {
            let mut result = "timeout";
            'poll: for _ in 0..200 { // 200 x 3 s = 10 minutes
                for _ in 0..30 { std::thread::sleep(Duration::from_millis(100)); if done.load(Ordering::SeqCst) { return; } }
                if let Ok((_, Some(w), _)) = read_wallet(&key) { if until(&w) { result = "done"; break 'poll; } }
            }
            *polled.lock().unwrap() = Some(result);
            done.store(true, Ordering::SeqCst);
        });
    }
    let action = ui.show(screen, done.clone());
    done.store(true, Ordering::SeqCst);
    let polled = *polled.lock().unwrap();
    action.or(polled).unwrap_or("retry")
}

/// The gate (see the top of the file). Returns (key, wallet, choice) once the person is signed in; choice is None when USES_AI is false.
pub fn ensure_signed_in(ui: &mut dyn GateUi) -> (String, Value, Option<String>) {
    if std::env::var_os("PUBLIK_SELF_TEST").is_some() { println!("{}", self_test()); std::process::exit(0); }
    let (mut sign_note, mut sign_poll, mut plan_note, mut plan_poll): (Option<&'static str>, bool, Option<&'static str>, bool) = (None, true, None, false);
    loop {
        let env_key = std::env::var("PUBLIK_API_KEY").ok().filter(|k| !k.is_empty());
        let c = match env_key.clone().map(|k| Ok(json!({ "key": k }))).or_else(|| read_credential().map(Ok)).unwrap_or_else(provision) {
            Ok(c) => c,
            Err(e) => { ask(ui, &gate_screen("unreachable", None, None, Some(e)), "", None); continue; }
        };
        let key = c["key"].as_str().unwrap_or_default().to_string();
        let w = match read_wallet(&key) {
            Err(_) => { ask(ui, &gate_screen("unreachable", None, None, None), "", None); continue; }
            Ok((401, _, _)) if env_key.is_none() => { let _ = write_credential(&json!({})); continue; } // a revoked key: provision again
            Ok((_, None, message)) => { ask(ui, &gate_screen("unreachable", None, None, message), "", None); continue; }
            Ok((_, Some(w), _)) => w,
        };
        if !claimed(&w) {
            let a = ask(ui, &gate_screen("sign_in", Some(&w), sign_note, None), &key, if sign_poll { Some(claimed) } else { None });
            if a == "sign_in" {
                ui.open_url(w["claim_url"].as_str().or(c["claim_url"].as_str()).unwrap_or(&format!("{}/wallet", origin())));
                (sign_note, sign_poll) = (Some("Waiting for you to sign in in your browser."), true);
            }
            if a == "timeout" { (sign_note, sign_poll) = (Some("No sign-in after 10 minutes. Click Sign in with publik to try again."), false); }
            continue;
        }
        let mut choice = if USES_AI { read_credential().and_then(|c| c["choice"].as_str().map(String::from)) } else { None };
        if USES_AI && choice.is_none() {
            if has_plan(&w) { (plan_note, plan_poll) = (None, false); } // the plan is there: Connect your plan
            let a = ask(ui, &gate_screen("choose", Some(&w), plan_note, None), &key, if plan_poll { Some(has_plan) } else { None });
            (plan_note, plan_poll) = (None, false);
            if a == "get_plan" {
                ui.open_url(w["top_up_url"].as_str().unwrap_or(&format!("{}/wallet", origin())));
                (plan_note, plan_poll) = (Some("Waiting for your plan. Finish in your browser."), true);
            }
            if a == "timeout" { plan_note = Some("No plan after 10 minutes. Click Get a plan to try again."); }
            if a != "connect" && a != "own_key" { continue; } // "done": the plan is there; the next screen is Connect your plan
            choice = Some(if a == "connect" { "publik" } else { "own_key" }.to_string());
            set_choice(choice.as_deref());
        }
        start_telemetry();
        return (key, w, choice);
    }
}

/// Builder check, run by the BUILT app: PUBLIK_SELF_TEST=1 makes ensure_signed_in() print this line and exit.
pub fn self_test() -> String {
    let result = provision().and_then(|c| read_wallet(c["key"].as_str().unwrap_or_default())).and_then(|(code, w, message)| match w {
        Some(_) => Ok(()),
        None => Err(message.unwrap_or_else(|| format!("GET /wallet answered {code}"))),
    });
    match result { Ok(()) => format!("publik self-test: ok {APP_SLUG} {APP_VERSION}"), Err(e) => format!("publik self-test: failed: {e}") }
}

/// A terminal UI for a command-line app. A desktop app passes its own GateUi (see the page).
pub struct TerminalUi;
impl GateUi for TerminalUi {
    fn show(&mut self, s: &Screen, done: Arc<AtomicBool>) -> Option<&'static str> {
        println!("\n{}\n{}{}", s.title, s.body, s.note.map(|n| format!("\n{n}")).unwrap_or_default());
        for (i, (_, label)) in s.buttons.iter().enumerate() { println!("  {}. {label}", i + 1); }
        let answered = Arc::new(AtomicBool::new(false));
        let (d, a) = (done.clone(), answered.clone());
        std::thread::spawn(move || { while !d.load(Ordering::SeqCst) { std::thread::sleep(Duration::from_millis(200)); } if !a.load(Ordering::SeqCst) { println!("\nDone. Press Enter to continue."); } });
        loop {
            print!("{}", if s.buttons.len() > 1 { "Type a number and press Enter: " } else { "Press Enter: " });
            let _ = std::io::stdout().flush();
            let mut line = String::new();
            if std::io::stdin().lock().read_line(&mut line).unwrap_or(0) == 0 { while !done.load(Ordering::SeqCst) { std::thread::sleep(Duration::from_millis(200)); } return None; }
            if done.load(Ordering::SeqCst) { return None; }
            let n: usize = line.trim().parse().unwrap_or(1);
            if let Some((action, _)) = s.buttons.get(n.wrapping_sub(1)) { answered.store(true, Ordering::SeqCst); return Some(action); }
        }
    }
    fn open_url(&mut self, url: &str) {
        println!("Open this link if your browser does not open: {url}");
        let _ = if cfg!(target_os = "macos") { Command::new("open").arg(url).spawn() }
            else if cfg!(windows) { Command::new("rundll32").args(["url.dll,FileProtocolHandler", url]).spawn() }
            else { Command::new("xdg-open").arg(url).spawn() };
    }
}

// ---------- usage counting: /skills/telemetry.md sections 3-5 ----------
static TELEMETRY_STARTED: AtomicBool = AtomicBool::new(false);

fn usage_off() -> bool {
    let env = |n: &str| std::env::var(n).unwrap_or_default().trim().to_lowercase();
    if !["", "0", "false"].contains(&env("DO_NOT_TRACK").as_str()) || ["0", "false", "off"].contains(&env("PUBLIK_TELEMETRY").as_str()) { return true; }
    std::fs::read_to_string(publik_dir().join("consent.json")).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()).map(|v| v["usage"] == false).unwrap_or(false)
}
fn is_uuid_v4(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 36 && b.iter().enumerate().all(|(i, c)| if [8, 13, 18, 23].contains(&i) { *c == b'-' } else { c.is_ascii_hexdigit() })
        && b[14] == b'4' && b"89abAB".contains(&b[19])
}
fn utc_now() -> String {
    let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let (days, rem) = ((secs / 86_400) as i64 + 719_468, secs % 86_400); // days since 0000-03-01
    let (era, doe) = (days / 146_097, days % 146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let (day, month) = (doy - (153 * mp + 2) / 5 + 1, if mp < 10 { mp + 3 } else { mp - 9 });
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z", yoe + era * 400 + i64::from(month <= 2), month, day, rem / 3600, rem % 3600 / 60, rem % 60)
}
/// One random UUID per computer account, shared by every publik app, in <publik dir>/telemetry.json (mode 0600).
fn telemetry_install_id() -> String {
    let file = publik_dir().join("telemetry.json");
    let saved = || std::fs::read_to_string(&file).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v["install_id"].as_str().map(str::to_lowercase)).filter(|id| is_uuid_v4(id));
    if let Some(id) = saved() { return id; }
    let fresh = uuid::Uuid::new_v4().to_string(); // if the file cannot be saved, this ID lasts for this run only
    let _ = std::fs::create_dir_all(publik_dir());
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true); // never over another app's file
    #[cfg(unix)]
    { use std::os::unix::fs::OpenOptionsExt; options.mode(0o600); }
    match options.open(&file) {
        Ok(mut f) => { let _ = writeln!(f, "{}", json!({ "install_id": fresh, "created_at": utc_now() })); fresh }
        Err(_) => saved().unwrap_or(fresh), // another publik app saved its ID first
    }
}
fn send_event(event: &str, install_id: &str) {
    let say = |result: &str| if std::env::var("PUBLIK_TELEMETRY_DEBUG").as_deref() == Ok("1") { eprintln!("publik-telemetry: {event} -> {result}"); };
    if usage_off() { return say("off (opt-out)"); }
    let key = std::env::var("PUBLIK_API_KEY").ok().or_else(|| read_credential().and_then(|c| c["key"].as_str().map(String::from))).filter(|k| k.starts_with("pk_"));
    let mut body = json!({ "v": 1, "app": APP_SLUG, "install_id": install_id, "event": event, "app_version": APP_VERSION.trim_start_matches(['v', 'V']), "os": OS });
    match std::env::consts::ARCH { "aarch64" => body["arch"] = json!("arm64"), "x86_64" => body["arch"] = json!("x64"), _ => {} }
    let url = std::env::var("PUBLIK_TELEMETRY_URL").unwrap_or_else(|_| format!("{}/api/v1/telemetry", origin()));
    let body = body.to_string();
    let args = ["-m", "5", "-K", "-", "-X", "POST", url.as_str(), "-H", "content-type: application/json", "--data-raw", body.as_str()];
    match curl(&args, &key.map(|k| key_config(&k)).unwrap_or_default()) { Ok((code, _)) => say(&code.to_string()), Err(e) => say(&e) }
}
/// Called by ensure_signed_in(). app_opened now, active_session every 30 minutes, on a background thread; never panics the app.
pub fn start_telemetry() {
    if TELEMETRY_STARTED.swap(true, Ordering::SeqCst) { return; }
    let s: u64 = std::env::var("PUBLIK_TELEMETRY_INTERVAL_SECONDS").ok().and_then(|v| v.parse().ok()).unwrap_or(0); // tests only
    let interval = Duration::from_secs(if (1..=86_400).contains(&s) { s } else { 1_800 });
    std::thread::spawn(move || {
        let id = telemetry_install_id();
        send_event("app_opened", &id);
        loop { std::thread::sleep(interval); send_event("active_session", &id); }
    });
}

// ---------- publik API calls ----------
/// A 402 renders its message plus exactly ONE link: this one.
pub fn top_up_url_from_402(body: &Value) -> (String, Option<String>) {
    let e = &body["error"];
    (e["message"].as_str().unwrap_or("Not enough publik API balance.").into(), e["top_up_url"].as_str().map(String::from))
}

/// `POST /search`: web results for `q` (quote a phrase to find it exactly), one published price per search; Err = (message, top_up_url) from a 402 or a readable failure.
pub fn search(base_url: &str, api_key: &str, q: &str, count: u8) -> Result<Vec<Value>, (String, Option<String>)> {
    let (url, body) = (format!("{base_url}/search"), json!({ "q": q, "count": count }).to_string());
    match curl(&["-m", "30", "-K", "-", "-X", "POST", &url, "-H", "content-type: application/json", "--data-raw", &body], &key_config(api_key)) {
        Ok((200, v)) => Ok(v["results"].as_array().cloned().unwrap_or_default()),
        Ok((402, v)) => Err(top_up_url_from_402(&v)),
        Ok((c, _)) => Err((format!("publik API answered {c}."), None)),
        Err(e) => Err((e, None)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_token_is_embedded() {
        assert!(APP_TOKEN.trim().starts_with("pat_publikclip_"));
    }

    #[test]
    fn the_gate_words() {
        let s = gate_screen("sign_in", None, None, None);
        assert_eq!(s.title, "Sign in with publik");
        assert_eq!(s.buttons, vec![("sign_in", "Sign in with publik")]);
        let u = gate_screen("unreachable", None, None, None);
        assert_eq!(u.body, "publik is not reachable. Check your connection.");
        assert_eq!(u.buttons, vec![("retry", "Retry")]);
        let empty = json!({ "claim_state": "claimed", "account": { "handle": "mann" }, "plan": { "kind": "none", "label": null }, "balance_micros": 0 });
        let c = gate_screen("choose", Some(&empty), None, None);
        assert_eq!(c.title, "Choose how to pay for AI");
        assert_eq!(c.body, "You are signed in as @mann. This app uses AI. Get a publik plan, or use your own key.");
        let plan = json!({ "claim_state": "claimed", "account": { "handle": "mann" }, "plan": { "kind": "plan", "label": "Plus" }, "balance_micros": 0 });
        let p = gate_screen("choose", Some(&plan), None, None);
        assert_eq!(p.title, "Connect your plan");
        assert_eq!(p.body, "You are signed in as @mann. Your plan: Plus. Connect it to use AI in this app, or use your own key.");
        assert_eq!(p.buttons, vec![("connect", "Connect your plan"), ("own_key", "Use my own key")]);
    }

    #[test]
    fn a_plan_or_a_balance_counts_as_a_plan() {
        assert!(!has_plan(&json!({ "plan": { "kind": "none" }, "balance_micros": 0 })));
        assert!(has_plan(&json!({ "plan": { "kind": "pack" }, "balance_micros": 0 })));
        assert!(has_plan(&json!({ "plan": { "kind": "none" }, "balance_micros": 1 })));
    }

    #[test]
    fn the_slug_and_the_version_are_this_app() {
        assert_eq!(APP_SLUG, "publikclip");
        assert_eq!(APP_VERSION, env!("CARGO_PKG_VERSION"));
        assert!(USES_AI);
    }
}
