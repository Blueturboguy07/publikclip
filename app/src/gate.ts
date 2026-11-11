// The gate window (gate.html): draws the screens of the publik module
// (src-tauri/src/publik.rs, through publik_gate.rs) and sends the button
// pressed back. It holds no state of its own: the Rust side says what to show.
import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'

interface GateScreen {
  id: string
  title: string
  body: string
  note: string | null
  buttons: { id: string; label: string }[]
}

const titleEl = document.getElementById('title')!
const bodyEl = document.getElementById('body')!
const noteEl = document.getElementById('note')!
const buttonsEl = document.getElementById('buttons')!

function render(screen: GateScreen) {
  document.title = screen.title
  titleEl.textContent = screen.title
  bodyEl.textContent = screen.body
  noteEl.textContent = screen.note ?? ''
  buttonsEl.replaceChildren()
  screen.buttons.forEach((button, i) => {
    const el = document.createElement('button')
    el.type = 'button'
    el.textContent = button.label
    if (i > 0) el.className = 'secondary'
    el.addEventListener('click', () => {
      for (const b of buttonsEl.querySelectorAll('button')) b.disabled = true
      invoke('publik_gate_press', { id: button.id }).catch(() => {
        for (const b of buttonsEl.querySelectorAll('button')) b.disabled = false
      })
    })
    buttonsEl.appendChild(el)
  })
}

listen<GateScreen>('publik-gate-screen', (event) => render(event.payload)).catch(() => {})
invoke<GateScreen | null>('publik_gate_ready')
  .then((screen) => screen && render(screen))
  .catch(() => {})
