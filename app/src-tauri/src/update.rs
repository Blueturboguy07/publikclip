// "A newer publikclip is out": one GET to GitHub's latest-release endpoint on
// launch, compared with the version compiled into this binary. publikclip has
// no self-updater, so without this a person on an old build never learns a
// fix shipped (the 0.2.2 builds kept showing a stale "$0.25 free" line for
// hours after the server had changed, 2026-09-28). Offline or rate-limited,
// it says nothing: a missing notice is never an error.
use serde_json::{json, Value};

use crate::publik::curl_request;

const LATEST_RELEASE_URL: &str = "https://api.github.com/repos/Blueturboguy07/publikclip/releases/latest";
const RELEASES_PAGE: &str = "https://github.com/Blueturboguy07/publikclip/releases/latest";

/// "v0.2.3" or "0.2.3" -> (0, 2, 3). Anything else is None, so a tag that
/// is not a version can never look newer.
pub(crate) fn parse_version(tag: &str) -> Option<(u64, u64, u64)> {
    let t = tag.trim().trim_start_matches('v');
    let mut parts = t.split('.');
    let major = parts.next()?.parse().ok()?;
    let minor = parts.next()?.parse().ok()?;
    let patch = parts.next()?.split('-').next()?.parse().ok()?;
    if parts.next().is_some() {
        return None;
    }
    Some((major, minor, patch))
}

pub(crate) fn newer(latest_tag: &str, current: &str) -> bool {
    match (parse_version(latest_tag), parse_version(current)) {
        (Some(l), Some(c)) => l > c,
        _ => false,
    }
}

/// The notice from a releases/latest answer. `html_url` is the release page
/// when GitHub gave one, else the releases index.
pub(crate) fn notice_from(current: &str, release: &Value) -> Value {
    let tag = release["tag_name"].as_str().unwrap_or("");
    let available = newer(tag, current);
    json!({
        "current": current,
        "latest": if tag.is_empty() { Value::Null } else { json!(tag.trim_start_matches('v')) },
        "update_available": available,
        "url": if available { release["html_url"].as_str().unwrap_or(RELEASES_PAGE) } else { RELEASES_PAGE },
    })
}

#[tauri::command]
pub async fn check_update() -> Result<Value, String> {
    let current = env!("CARGO_PKG_VERSION");
    let quiet = json!({ "current": current, "latest": Value::Null, "update_available": false, "url": RELEASES_PAGE });
    match curl_request("GET", LATEST_RELEASE_URL, None, None, 8) {
        Ok((200, body)) => Ok(notice_from(current, &body)),
        _ => Ok(quiet),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_tags_with_and_without_the_v() {
        assert_eq!(parse_version("v0.2.3"), Some((0, 2, 3)));
        assert_eq!(parse_version("0.10.0"), Some((0, 10, 0)));
        assert_eq!(parse_version("v1.0.0-rc1"), Some((1, 0, 0)));
        assert_eq!(parse_version("latest"), None);
        assert_eq!(parse_version("v0.2"), None);
    }

    #[test]
    fn newer_compares_numerically_not_lexically() {
        assert!(newer("v0.2.3", "0.2.2"));
        assert!(newer("v0.10.0", "0.9.9"));
        assert!(!newer("v0.2.2", "0.2.2"));
        assert!(!newer("v0.2.1", "0.2.2"));
        assert!(!newer("nightly", "0.2.2"));
    }

    #[test]
    fn a_newer_release_carries_its_page_and_an_older_one_is_quiet() {
        let n = notice_from("0.2.2", &json!({"tag_name": "v0.2.3", "html_url": "https://github.com/x/y/releases/tag/v0.2.3"}));
        assert_eq!(n["update_available"], true);
        assert_eq!(n["latest"], "0.2.3");
        assert_eq!(n["url"], "https://github.com/x/y/releases/tag/v0.2.3");
        let q = notice_from("0.2.3", &json!({"tag_name": "v0.2.3", "html_url": "u"}));
        assert_eq!(q["update_available"], false);
        assert_eq!(q["latest"], "0.2.3");
        let e = notice_from("0.2.3", &json!({}));
        assert_eq!(e["update_available"], false);
        assert!(e["latest"].is_null());
    }
}
