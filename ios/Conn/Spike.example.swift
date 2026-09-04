import Foundation

/// Copy to `Spike.swift` and fill in. That file is gitignored: it names a real
/// host and holds a real credential.
///
/// A password is the quickest way to get the spike talking. Swap to
/// `.privateKey` before deciding anything about Citadel, since key auth is the
/// half that has to work and the half most likely to disappoint.
enum Spike {
    static let target = SSHTarget(
        host: "selectbooth",             // a real hostname or IP, not an ssh alias:
                                         // ~/.ssh/config does not exist on iOS
        port: 22,
        user: "ojm",
        auth: .password("..."),
        // auth: .privateKey(pem: privateKeyPEM, passphrase: nil),
        session: "spike"
    )

    /// Paste an OpenSSH private key here when moving off passwords. Ed25519:
    /// begins "-----BEGIN OPENSSH PRIVATE KEY-----".
    static let privateKeyPEM = """
    -----BEGIN OPENSSH PRIVATE KEY-----
    -----END OPENSSH PRIVATE KEY-----
    """
}
