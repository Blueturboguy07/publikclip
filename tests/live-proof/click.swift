import CoreGraphics
import Foundation
// click.swift <x> <y> [right] | type <text> | key <code>
// Real input events, as a person's hand makes them. macOS refuses the accessibility API for an app
// whose file on disk was replaced while it runs (the state after "Later"); the mouse still works.
let a = CommandLine.arguments
func key(_ code: CGKeyCode, _ text: String?) {
  for down in [true, false] {
    let e = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down)!
    if let t = text { let u = Array(t.utf16); e.keyboardSetUnicodeString(stringLength: u.count, unicodeString: u) }
    e.post(tap: .cghidEventTap)
    usleep(60000)
  }
}
if a[1] == "type" {
  for c in a[2] { key(0, String(c)) }
} else if a[1] == "key" {
  key(CGKeyCode(UInt16(a[2])!), nil)
} else {
  let p = CGPoint(x: Double(a[1])!, y: Double(a[2])!)
  let right = a.count > 3 && a[3] == "right"
  let down = CGEvent(mouseEventSource: nil, mouseType: right ? .rightMouseDown : .leftMouseDown, mouseCursorPosition: p, mouseButton: right ? .right : .left)!
  down.post(tap: .cghidEventTap)
  usleep(120000)
  let up = CGEvent(mouseEventSource: nil, mouseType: right ? .rightMouseUp : .leftMouseUp, mouseCursorPosition: p, mouseButton: right ? .right : .left)!
  up.post(tap: .cghidEventTap)
}
