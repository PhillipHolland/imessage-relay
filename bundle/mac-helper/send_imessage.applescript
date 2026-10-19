-- iMessage Relay: sends ONE message (text, or one file) to ONE participant (1:1) or to ONE group chat.
-- Payload, handle (or chat GUID) and service arrive as argv, so nothing from the message is ever
-- interpolated into script source.
-- Usage: osascript send_imessage.applescript "<text or POSIX path>" "<handle or chat GUID>" ["iMessage"|"SMS"|"auto"] ["participant"|"chat"] ["text"|"file"]
-- service "auto" (default) tries iMessage first, then SMS (needs Text Message Forwarding).
-- v1.4.1: a 4th argument "chat" sends to the group chat whose GUID is given ("iMessage;+;chat123..."),
-- the same chat the group's messages came from (the service argument is then ignored).
-- v1.4.6: a 5th argument "file" sends the file at the POSIX path in the 1st argument (an attachment the
-- relay staged under ~/Pictures/.imessage-relay-out, a folder Messages can read).
-- Works with Messages quit (it launches in the background, ~3 s) or hidden (~1 s).
on run argv
	if (count of argv) < 2 then error "expected: payload handle [service] [participant|chat] [text|file]"
	set thePayload to item 1 of argv
	set theHandle to item 2 of argv
	set theService to "auto"
	if (count of argv) is greater than or equal to 3 then set theService to item 3 of argv
	set toChat to false
	if (count of argv) is greater than or equal to 4 then
		if item 4 of argv is "chat" then set toChat to true
	end if
	set theMode to "text"
	if (count of argv) is greater than or equal to 5 then set theMode to item 5 of argv
	if theMode is "file" then
		-- resolved outside the Messages tell block: a missing file fails here, before anything is sent
		set theItem to (POSIX file thePayload) as alias
	else
		set theItem to thePayload
	end if
	if toChat then
		tell application "Messages"
			send theItem to chat id theHandle
		end tell
		return
	end if
	tell application "Messages"
		if theService is "iMessage" then
			send theItem to participant theHandle of (1st account whose service type = iMessage)
		else if theService is "SMS" then
			my sendSMS(theItem, theHandle)
		else
			-- auto: try iMessage once. Re-raise the control errors unchanged so the host classifies them
			-- (never double-send on busy/automation). Fall back to SMS ONLY on a genuine no-iMessage route.
			try
				send theItem to participant theHandle of (1st account whose service type = iMessage)
			on error errMsg number errNum
				if errNum is -1712 or errNum is -1743 or errNum is -1728 then
					error errMsg number errNum
				else if (errMsg contains "iMessage") or (errMsg contains "participant") or (errMsg contains "not registered") or (errMsg contains "service") then
					my sendSMS(theItem, theHandle)
				else
					error errMsg number errNum
				end if
			end try
		end if
	end tell
end run

on sendSMS(theItem, theHandle)
	tell application "Messages"
		try
			set smsAccount to 1st account whose service type = SMS
		on error
			error "SMS replies not supported (no SMS service; enable Text Message Forwarding)"
		end try
		send theItem to participant theHandle of smsAccount
	end tell
end sendSMS
