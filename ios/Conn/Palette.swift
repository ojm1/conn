import SwiftUI

/// Flexoki, read off the desktop's live theme so the two halves of conn look
/// like one product. These are the resolved values from
/// ~/.local/state/omarchy/current/theme/colors.toml, not an approximation of
/// the screenshot.
///
/// v1 reads the palette from the vault instead, so changing the desktop theme
/// changes the phone. Hard-coded here because the spike has no vault.
enum Palette {
    static let paper = Color(hex: 0xFFFCF0)
    static let panel = Color(hex: 0xE5E2D8)
    static let rule = Color(hex: 0xCECDC3)
    static let ink = Color(hex: 0x100F0F)
    static let muted = Color(hex: 0x878580)
    static let accent = Color(hex: 0x205EA6)

    /// The three alert conditions, same as the desktop marks.
    static let needsYou = Color(hex: 0xD14D41)
    static let working = Color(hex: 0xD0A215)
    static let clear = Color(hex: 0x879A39)
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255,
                  opacity: 1)
    }
}
