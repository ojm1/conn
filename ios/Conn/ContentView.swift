import SwiftUI

/// One session, full screen. That is the whole spike.
///
/// No host list, no vault, no session picker: milestone 1 asks one question --
/// do bytes go both ways and does the geometry hold -- and every screen added
/// before that is answered is a screen built on an assumption.
struct ContentView: View {
    @State private var status: ConnectionStatus = .idle
    @State private var transport = CitadelTransport()

    var body: some View {
        ZStack(alignment: .top) {
            Palette.paper.ignoresSafeArea()

            TerminalHost(transport: transport, target: Spike.target,
                         status: $status)
                .ignoresSafeArea(.container, edges: .bottom)

            if status != .open {
                banner
            }
        }
    }

    private var banner: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(dot)
                .frame(width: 8, height: 8)
            Text(message)
                .font(.system(size: 13, design: .monospaced))
                .foregroundStyle(Palette.ink)
            Spacer()
            if case .closed = status {
                Button("Reconnect") {
                    Task {
                        await transport.close()
                        transport = CitadelTransport()
                        status = .idle
                    }
                }
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(Palette.accent)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(Palette.panel)
        .overlay(alignment: .bottom) {
            Rectangle().fill(Palette.rule).frame(height: 1)
        }
    }

    private var dot: Color {
        switch status {
        case .open: return Palette.clear
        case .connecting, .idle: return Palette.working
        case .closed: return Palette.needsYou
        }
    }

    private var message: String {
        switch status {
        case .idle: return "not connected"
        case .connecting: return "connecting to \(Spike.target.host)"
        case .open: return "\(Spike.target.host)/\(Spike.target.session)"
        // The session is tmux, so nothing was lost -- say that, rather than
        // reporting a disconnection as if work had gone with it.
        case .closed(let why):
            return why.map { "disconnected: \($0)" } ?? "disconnected -- the session is still running"
        }
    }
}
