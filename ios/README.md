# conn for iOS — milestone 1

The spike. One hardcoded host, one tmux session, full screen, and nothing else:
milestone 1 asks a single question — **do bytes go both ways, and does the
geometry hold when the device rotates** — and every screen built before that is
answered rests on an assumption.

Universal, so the iPhone gets it too. The iPad is the reason it exists; the
iPhone is the reason it exists *now*.

## Build it

On the Mac mini:

```bash
brew install xcodegen
cd ios
cp Conn/Spike.example.swift Conn/Spike.swift   # fill in host, user, credential
xcodegen                                        # writes Conn.xcodeproj
open Conn.xcodeproj
```

`Conn.xcodeproj` is generated and gitignored — the desktop half of conn is
developed on Linux, where it cannot be opened, so `project.yml` is the project.
Regenerate after changing dependencies; never edit the pbxproj.

`Spike.swift` is gitignored too. It names a real host and holds a real
credential.

> `~/.ssh/config` does not exist on iOS, so `Spike.swift` needs a real hostname
> or IP — an alias like `selectbooth` resolves on the laptop and nowhere else.

## What to check, in this order

1. **It connects and echoes.** Type, see characters. That is the whole first
   hurdle.
2. **Key auth, not just a password.** Ed25519 specifically. This is the half
   most likely to disappoint and the half v1 depends on, because a synced key
   is what makes a second device cheap — see the vault section of the plan.
3. **Rotate the device.** Full-screen programs must reflow. If they do not,
   `changeSize` is not reaching the far side and the fix is in
   `CitadelTransport.resize`.
4. **Background the app for a minute, come back.** iOS takes the socket and
   there is no entitlement that changes it. What matters is that the banner
   says so and Reconnect returns you to the same tmux session with the work
   still running — that is conn's whole thesis, and it either holds here or the
   design is wrong.

## The one file expected to fight back

`SSH/CitadelTransport.swift`. Citadel sits on Apple's SwiftNIO SSH and its
interactive-shell API has moved between releases, so the call signatures there
are a first draft against whatever version resolves. Expect to fix them; that
is what a spike is for.

Everything else talks to `SSHTransport`, which is four methods. If Citadel
disappoints, the replacements are NIOSSH directly (more code, same package) or
libssh2 through a bridging header, which is what Blink uses — and nothing above
the protocol changes either way.

## What is deliberately absent

The vault, the host list, the session picker, the extra key row, host-key
pinning, and any Keychain use. All of it is v1, all of it is in the plan, and
none of it should be written until the four checks above pass.

`hostKeyValidator: .acceptAnything()` is a spike affordance. v1 pins on first
use and keeps the fingerprint in the vault, so a changed host key is a question
rather than a shrug.
