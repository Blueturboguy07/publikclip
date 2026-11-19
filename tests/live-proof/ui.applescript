-- UI helper for the macOS live update proof (tests/live-proof/proof-macos.sh). Runner only.
-- System Events reads and presses another app's windows and menus (GitHub's macOS runners let
-- osascript use accessibility).
--
--   osascript ui.applescript count <pid|name> <process>           number of windows
--   osascript ui.applescript texts <pid|name> <process>           every window's title and texts
--   osascript ui.applescript press <pid|name> <process> <button>  click the button in a window
--   osascript ui.applescript menu  pid <pid> <item prefix>        click the item of the app menu
-- A missing process by name gives "" (count, texts) or an error (press).

on run argv
	set cmd to item 1 of argv
	set kind to item 2 of argv
	set what to item 3 of argv
	tell application "System Events"
		if kind is "pid" then
			set p to first process whose unix id is (what as integer)
		else
			if not (exists process what) then
				if cmd is "press" then error "no process " & what
				return ""
			end if
			set p to process what
		end if
		if cmd is "count" then
			return (count windows of p) as text
		else if cmd is "texts" then
			set out to {}
			repeat with w in windows of p
				try
					set t to name of w
					if t is not missing value and t is not "" then set end of out to (t as text)
				end try
				set found to false
				try
					repeat with s in (static texts of w)
						set v to value of s
						if v is not missing value and v is not "" then
							set end of out to (v as text)
							set found to true
						end if
					end repeat
				end try
				if not found then
					-- Texts that sit deeper (a group or a scroll area).
					try
						repeat with e in (entire contents of w)
							if role of e is "AXStaticText" then
								set v to value of e
								if v is not missing value and v is not "" then set end of out to (v as text)
							end if
						end repeat
					end try
				end if
			end repeat
			set AppleScript's text item delimiters to " | "
			return out as text
		else if cmd is "press" then
			set b to item 4 of argv
			repeat with w in windows of p
				if exists button b of w then
					click button b of w
					return "pressed " & b
				end if
			end repeat
			error "no button " & b
		else if cmd is "menu" then
			set prefix to item 4 of argv
			set frontmost of p to true
			delay 1
			-- Menu bar item 1 is the Apple menu; item 2 is the app menu (Tauri's default menu).
			set mb to menu bar item 2 of menu bar 1 of p
			set names to {}
			repeat with mi in menu items of menu 1 of mb
				set nm to ""
				try
					set nm to name of mi as text
				end try
				set end of names to nm
				if nm starts with prefix then
					click mi
					return "clicked '" & nm & "' in the app menu '" & (name of mb) & "'"
				end if
			end repeat
			set AppleScript's text item delimiters to ", "
			error "no item " & prefix & " in the app menu " & (name of mb) & ": " & (names as text)
		end if
		error "unknown command " & cmd
	end tell
end run
