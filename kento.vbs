' kento.vbs - start KENTO without a black console window.
' If it does not start, open kento_error.txt in the same folder.

Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appdir  = fso.GetParentFolderName(WScript.ScriptFullName)
script  = appdir & "\app.py"
logfile = appdir & "\kento_error.txt"

If Not fso.FileExists(script) Then
  MsgBox "app.py not found:" & vbCrLf & script, 16, "KENTO"
  WScript.Quit 1
End If

sh.CurrentDirectory = appdir

q = Chr(34)
cmd = "cmd /c " & q & "pythonw " & q & script & q & " >" & q & logfile & q & " 2>&1" & q
sh.Run cmd, 0, False
