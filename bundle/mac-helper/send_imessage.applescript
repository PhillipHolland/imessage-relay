-- iMessage Relay: sends ONE message to ONE participant (1:1). Text, handle and service arrive as
-- argv, so nothing from the message is ever interpolated into script source.
-- Usage: osascript send_imessage.applescript "<text>" "<handle>" ["iMessage"|"SMS"|"auto"]
-- service "auto" (default) tries iMessage first, then SMS (needs Text Message Forwarding).
-- Works with Messages quit (it launches in the background, ~3 s) or hidden (~1 s).
on run argv
	if (count of argv) < 2 then error "expected: text handle [service]"
	set theText to item 1 of argv
	set theHandle to item 2 of argv
	set theService to "auto"
	if (count of argv) is greater than or equal to 3 then set theService to item 3 of argv
	tell application "Messages"
		if theService is "iMessage" then
			send theText to participant theHandle of (1st account whose service type = iMessage)
		else if theService is "SMS" then
			my sendSMS(theText, theHandle)
		else
			-- auto: try iMessage once. Re-raise the control errors unchanged so the host classifies them
			-- (never double-send on busy/automation). Fall back to SMS ONLY on a genuine no-iMessage route.
			try
				send theText to participant theHandle of (1st account whose service type = iMessage)
			on error errMsg number errNum
				if errNum is -1712 or errNum is -1743 or errNum is -1728 then
					error errMsg number errNum
				else if (errMsg contains "iMessage") or (errMsg contains "participant") or (errMsg contains "not registered") or (errMsg contains "service") then
					my sendSMS(theText, theHandle)
				else
					error errMsg number errNum
				end if
			end try
		end if
	end tell
end run

on sendSMS(theText, theHandle)
	tell application "Messages"
		try
			set smsAccount to 1st account whose service type = SMS
		on error
			error "SMS replies not supported (no SMS service; enable Text Message Forwarding)"
		end try
		send theText to participant theHandle of smsAccount
	end tell
end sendSMS
