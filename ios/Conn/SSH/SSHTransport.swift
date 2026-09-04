import Foundation

/// What the terminal needs from a connection, and nothing else.
///
/// The terminal does not care whether the bytes come from Citadel, from
/// SwiftNIO SSH underneath it, or from libssh2 through a bridging header --
/// and that matters, because which of those we end up on is precisely the
/// question this spike exists to answer. Everything above this protocol is
/// written once; only the thing implementing it is at risk.
protocol SSHTransport: AnyObject {
    /// Bytes arriving from the far side, delivered on the main actor because
    /// the only thing that ever consumes them is a view.
    var onOutput: (@MainActor (ArraySlice<UInt8>) -> Void)? { get set }

    /// The connection ending, for any reason: the far side hanging up, the
    /// network going away, the app being backgrounded long enough for iOS to
    /// take the socket. A terminal that does not know it has been cut off
    /// looks exactly like one that is idle.
    var onClose: (@MainActor (Error?) -> Void)? { get set }

    func connect(to target: SSHTarget, size: TerminalSize) async throws
    func send(_ bytes: ArraySlice<UInt8>) async
    func resize(to size: TerminalSize) async
    func close() async
}

struct SSHTarget {
    var host: String
    var port: Int = 22
    var user: String
    var auth: Auth
    /// The tmux session to attach to, created if it is not there. This is the
    /// whole reason conn survives a dropped connection: the work is on the far
    /// side, and reattaching costs a round trip rather than a session.
    var session: String

    enum Auth {
        case password(String)
        case privateKey(pem: String, passphrase: String?)
    }

    /// `new -A` is attach-or-create in one word, so a first connection and a
    /// reconnection are the same command. `-u` forces UTF-8 regardless of what
    /// the login shell claims the locale is; iOS sends none.
    var attachCommand: String {
        let safe = session.replacingOccurrences(
            of: "[^A-Za-z0-9_-]", with: "_", options: .regularExpression)
        return "tmux -u new -A -s \(safe.isEmpty ? "shell" : safe)"
    }
}

struct TerminalSize: Equatable {
    var columns: Int
    var rows: Int
    /// Pixel dimensions are optional in the protocol and most servers ignore
    /// them, but programs that draw images (sixel, kitty graphics) read them.
    var pixelWidth: Int = 0
    var pixelHeight: Int = 0
}

enum SSHTransportError: LocalizedError {
    case notConnected
    case handshakeFailed(String)

    var errorDescription: String? {
        switch self {
        case .notConnected:
            return "Not connected."
        case .handshakeFailed(let why):
            return why
        }
    }
}
