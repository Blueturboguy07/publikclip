fn main() {
    // The publik app token is a compile-time constant (src/publik.rs). Without
    // these two lines cargo's fingerprint would keep a stale value and a
    // rotation would ship as "the old token", which is a failure that looks
    // like a server-side revocation and is debugged as one.
    println!("cargo:rerun-if-env-changed=PUBLIK_APP_TOKEN");
    println!("cargo:rerun-if-changed=publik-app-token.txt");
    tauri_build::build()
}
