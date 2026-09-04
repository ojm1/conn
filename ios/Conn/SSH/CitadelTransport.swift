import Foundation
import Citadel
import NIOCore
import NIOSSH

/// The one file in this spike that is expected to fight back.
///
/// Citadel sits on Apple's SwiftNIO SSH and its interactive-shell API has
/// moved between releases, so treat the call signatures below as a first
/// draft against whatever version resolves -- that is what milestone 1 is
/// for. If Ed25519 auth or PTY resize turn out to be missing or awkward, the
/// fallback is NIOSSH directly (more code, same package) or libssh2 through a
/// bridging header, which is what Blink uses. Nothing above SSHTransport
/// changes either way; that is why the protocol is there.
final class CitadelTransport: SSHTransport {
    var onOutput: (@MainActor (ArraySlice<UInt8>) -> Void)?
    var onClose: (@MainActor (Error?) -> Void)?

    private var client: SSHClient?
    private var stdin: TTYStdinWriter?
    private var pumping: Task<Void, Never>?

    func connect(to target: SSHTarget, size: TerminalSize) async throws {
        let method: SSHAuthenticationMethod
        switch target.auth {
        case .password(let secret):
            method = .passwordBased(username: target.user, password: secret)
        case .privateKey(let pem, let passphrase):
            // Ed25519 by preference: it is what ssh-keygen has produced by
            // default for years, and the reason the vault carries the key
            // rather than the Secure Enclave holding it -- an Enclave key
            // cannot be exported, which is the whole point of one, and
            // therefore cannot be synced to a second device.
            let key = try Curve25519.Signing.PrivateKey(
                sshEd25519: pem, decryptionKey: passphrase.map { Data($0.utf8) })
            method = .ed25519(username: target.user, privateKey: key)
        }

        let client = try await SSHClient.connect(
            host: target.host,
            port: target.port,
            authenticationMethod: method,
            // Accept-anything is a spike affordance, not a shipping one. v1
            // pins the host key on first use and stores it in the vault, so a
            // changed key is a question rather than a shrug.
            hostKeyValidator: .acceptAnything(),
            reconnect: .never
        )
        self.client = client

        let tty = try await client.withPTY(
            .init(wantReply: true,
                  term: "xterm-256color",
                  terminalCharacterWidth: size.columns,
                  terminalRowHeight: size.rows,
                  terminalPixelWidth: size.pixelWidth,
                  terminalPixelHeight: size.pixelHeight,
                  terminalModes: .init([.ECHO: 1, .ICRNL: 1, .IXON: 1])),
            // The command is the attach, not a login shell: whatever the far
            // side would otherwise put on the screen is noise in front of a
            // session that already exists.
            command: target.attachCommand
        )
        self.stdin = tty.stdin

        pumping = Task { [weak self] in
            do {
                for try await chunk in tty.stdout {
                    let bytes = Array(buffer: chunk)[...]
                    await MainActor.run { self?.onOutput?(bytes) }
                }
                await MainActor.run { self?.onClose?(nil) }
            } catch {
                await MainActor.run { self?.onClose?(error) }
            }
        }
    }

    func send(_ bytes: ArraySlice<UInt8>) async {
        guard let stdin else { return }
        try? await stdin.write(ByteBuffer(bytes: bytes))
    }

    func resize(to size: TerminalSize) async {
        guard let stdin else { return }
        // The half most likely to be missing, and the half you notice
        // immediately: without it, rotating the iPad leaves every full-screen
        // program drawing to the old geometry.
        try? await stdin.changeSize(cols: size.columns, rows: size.rows,
                                    pixelWidth: size.pixelWidth,
                                    pixelHeight: size.pixelHeight)
    }

    func close() async {
        pumping?.cancel()
        pumping = nil
        stdin = nil
        try? await client?.close()
        client = nil
    }
}
