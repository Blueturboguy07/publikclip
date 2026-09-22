fn main() {
    // The publik API app token is compiled in (src/publik.rs); without these a
    // rotated token file or env override would keep cargo's stale build.
    println!("cargo:rerun-if-env-changed=PUBLIK_APP_TOKEN");
    println!("cargo:rerun-if-changed=publik-app-token.txt");
    tauri_build::build()
}
