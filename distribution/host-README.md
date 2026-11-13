# NyXion Heredity Alpha Build 26.11.13 - Core Revision 1

This archive contains the independent host, its built WebUI and a separate SDK
wheel. It contains no platform plugin, login material, local configuration or native
platform component. The package version is `26.11.13.post1`; the product release
label is `Alpha Build 26.11.13 (Core Revision 1)`.

Python 3.11 or newer is required. From this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --find-links wheels nyxion-heredity==26.11.13.post1
.\.venv\Scripts\heredity.exe app
```

On Linux, use `.venv/bin/python` and `.venv/bin/heredity`. The dependency wheels
for WebSockets and Wasmtime are obtained through pip for the current platform.
This is a Python source/wheel release, not a standalone executable installer.

The UI starts on `127.0.0.1:5099`. To change it, create `.env` from `.env.example`,
then set `HEREDITY_UI_PORT`. No OneBot listener or account is created by default.
Install a platform plugin separately, then create interfaces in the WebUI.

The generated UI password is stored under `.heredity/host/ui-password.txt`.
That directory and your `.env` are private runtime data; do not upload them.
Aegis isolates marketplace WASM modules. Trusted embedded platform plugins use
their declared Python runtime and are not represented as WASM-isolated.

The included `src/` tree and `pyproject.toml` can also be installed using
`python -m pip install --find-links wheels .`.
