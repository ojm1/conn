import SwiftUI
import SwiftTerm

/// SwiftTerm's terminal, wrapped so SwiftUI can hold it.
///
/// The terminal itself stays UIKit. There is no SwiftUI terminal worth having
/// -- this is the one screen where a decade of edge cases (selection, IME,
/// hardware modifiers, scrollback) is the product -- so the wrapper's only job
/// is to hand bytes across and stay out of the way.
struct TerminalHost: UIViewRepresentable {
    let transport: SSHTransport
    let target: SSHTarget
    @Binding var status: ConnectionStatus

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> TerminalView {
        let view = TerminalView(frame: .zero)
        view.terminalDelegate = context.coordinator
        view.backgroundColor = UIColor(Palette.paper)
        view.nativeForegroundColor = UIColor(Palette.ink)
        view.nativeBackgroundColor = UIColor(Palette.paper)
        // A hardware keyboard is the assumption on iPad; on a phone the extra
        // key row goes here, as an input accessory. Neither exists yet: the
        // spike is about whether bytes arrive and geometry holds.
        context.coordinator.attach(to: view)
        return view
    }

    func updateUIView(_ view: TerminalView, context: Context) {}

    @MainActor
    final class Coordinator: NSObject, TerminalViewDelegate {
        private let parent: TerminalHost
        private weak var view: TerminalView?
        private var started = false

        init(_ parent: TerminalHost) { self.parent = parent }

        func attach(to view: TerminalView) {
            self.view = view
            parent.transport.onOutput = { [weak view] bytes in
                view?.feed(byteArray: bytes)
            }
            parent.transport.onClose = { [weak self] error in
                // iOS takes the socket seconds after backgrounding and there
                // is no entitlement that changes that. It is not an error
                // worth a dialog: the session is tmux and is still running, so
                // this says what happened and offers the way back.
                self?.parent.status = .closed(error?.localizedDescription)
            }
            guard !started else { return }
            started = true
            Task { await connect(size: view.getTerminal().getDims()) }
        }

        private func connect(size: (cols: Int, rows: Int)) async {
            parent.status = .connecting
            do {
                try await parent.transport.connect(
                    to: parent.target,
                    size: TerminalSize(columns: size.cols, rows: size.rows))
                parent.status = .open
            } catch {
                parent.status = .closed(error.localizedDescription)
            }
        }

        // -- TerminalViewDelegate -------------------------------------------

        func send(source: TerminalView, data: ArraySlice<UInt8>) {
            Task { await parent.transport.send(data) }
        }

        func sizeChanged(source: TerminalView, newCols: Int, newRows: Int) {
            Task {
                await parent.transport.resize(
                    to: TerminalSize(columns: newCols, rows: newRows))
            }
        }

        func setTerminalTitle(source: TerminalView, title: String) {}
        func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}
        func scrolled(source: TerminalView, position: Double) {}
        func rangeChanged(source: TerminalView, startY: Int, endY: Int) {}
        func clipboardCopy(source: TerminalView, content: Data) {
            UIPasteboard.general.string = String(data: content, encoding: .utf8)
        }
        func requestOpenLink(source: TerminalView, link: String, params: [String: String]) {
            guard let url = URL(string: link) else { return }
            UIApplication.shared.open(url)
        }
        func bell(source: TerminalView) {}
        func iTermContent(source: TerminalView, content: ArraySlice<UInt8>) {}
    }
}

enum ConnectionStatus: Equatable {
    case idle
    case connecting
    case open
    case closed(String?)
}
