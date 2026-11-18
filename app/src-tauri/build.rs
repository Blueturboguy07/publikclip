fn main() {
    // The publik API app token is compiled in (src/publik.rs); without these a
    // rotated token file or env override would keep cargo's stale build.
    println!("cargo:rerun-if-env-changed=PUBLIK_APP_TOKEN");
    println!("cargo:rerun-if-changed=publik-app-token.txt");
    // Test builds point the updater at a test feed (src/updates.rs, option_env!).
    println!("cargo:rerun-if-env-changed=PUBLIK_UPDATE_ENDPOINT");
    tauri_build::build()
}
