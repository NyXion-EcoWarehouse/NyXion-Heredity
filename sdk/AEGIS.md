# Aegis Module ABI 1.0

Aegis runs an untrusted WebAssembly module in its own Wasmtime worker process. The host executes no Python, JavaScript, DLL, ELF, APK, shell, or native executable from a marketplace archive. Windows and Unix use the same WebAssembly ABI; this is a Wasmtime VM plus a separate process, not a claim that the Python worker itself runs inside an AppContainer, container, or Linux namespace.

Embedded platform plugins that load native signing binaries remain trusted code. They cannot be represented as isolated WASM packages without migration. Their status must not claim Aegis isolation.

## Package

A ZIP contains exactly `manifest.json` and `module.wasm`. The installer verifies its expected SHA-256 before installation. A bare WASM may receive trusted installation metadata from a verified marketplace index; this does not establish a guest-supplied identity.

```json
{
  "schema_version": 1,
  "id": "example.adapter",
  "name": "Example adapter",
  "version": "1.0.0",
  "sdk_api": {"major": 1, "minor": 0},
  "min_heredity_version": "0.3.0",
  "permissions": ["fs.read", "fs.write", "bus.call"],
  "module": "module.wasm"
}
```

`SandboxModuleManifest` in `nyxion_heredity_sdk.sandbox` validates these exact fields. The manifest declares the maximum permissions a package may request. Installation or permission approval does not grant undeclared capabilities. The host supplies an approved subset to the runtime; no runtime permission comes from a guest handshake.

## Exports

The guest exports one 32-bit `memory`, `aegis_alloc(i32 length) -> i32 pointer`, and `aegis_call(i32 pointer, i32 length) -> i64`. There are no shared memories, threads, memory64, extra memories, components, or arbitrary imports.

The host writes UTF-8 JSON at the allocated pointer:

```json
{"method": "get_status", "params": {}}
```

The returned i64 packs the response pointer in its unsigned high 32 bits and its length in its low 32 bits. The response must be a JSON object inside guest memory. Buffers and individual IPC frames are at most 256 KiB; JSON is limited to depth 24 and 16,384 nodes. The host performs one call at a time for each module and never retries a business operation.

The first method is `handshake`, with `{"api_major":1,"api_minor":0,"module_id":"example.adapter"}`. The guest response must include these exact values. A mismatch terminates the worker. The host assigns the module's namespace, normally `/modules/example.adapter`; a package cannot select another module's namespace.

## Capabilities

The only non-WASI import is:

```text
nyxion_aegis.host_call(
  i32 request_pointer, i32 request_length,
  i32 output_pointer, i32 output_capacity
) -> i32 response_length
```

The input JSON has exactly `capability`, `operation`, and `params`. A response is a JSON object or other JSON value supplied by the host broker. A denied permission, malformed capability request, out-of-scope path, or denied bus destination terminates and reaps the worker. The runtime reports a fixed error code and never includes private request data in its error report.

| Capability | Operation | Params |
| --- | --- | --- |
| `fs.read` | `fs.read` | `{"path":"relative/file.txt"}` |
| `fs.write` | `fs.write` | `{"path":"relative/file.txt","data_base64":"..."}` |
| `net.connect` | `tcp.connect` | `{"host":"example.org","port":443}` |
| `net.connect` | `tcp.write` | `{"handle":"opaque","data_base64":"..."}` |
| `net.connect` | `tcp.read` | `{"handle":"opaque","max_bytes":65536}` |
| `net.connect` | `tcp.close` | `{"handle":"opaque"}` |
| `net.connect` | `https.request` | `{"url":"https://example.org/path","method":"GET"}` |
| `bus.call` | `tree.self` | `{}` |
| `bus.call` | `interfaces.declare` | `{"transport_types":["http_server"],"directions":["call","receive"],"account":"example:12345678"}` |
| `bus.call` | `onebot.call` | `{"interface_id":"0123456789abcdef","action":"get_status","params":{}}` |
| `bus.publish` | `onebot.event` | `{"interface_id":"0123456789abcdef","event":{"self_id":12345678,"post_type":"meta_event"}}` |
| `bus.subscribe` | `onebot.receive` | `{"interface_id":"0123456789abcdef"}` |

The `example:` account namespace above illustrates syntax only. Use the actual
platform namespace supplied by the selected plugin; existing account identities
are never renamed to this example.

File data and network data are limited to 64 KiB per operation. Files are confined to this module's private state directory. Absolute paths, `..`, links, junctions, hard-linked files, alternate streams, and special files are rejected. The guest receives no filesystem preopens or host file descriptors; authorized file access goes through the broker.

Networking is denied by default, including when a manifest declares `net.connect`. The host must explicitly approve the capability and configure destinations. Exact `tcp://host:port` or `https://host:port` allowlist entries authorize those origins. The host's `*public*` policy authorizes only public IP destinations: all DNS answers are checked, the selected numeric address is used for the actual connection, HTTPS retains the original hostname for TLS verification, and redirects are not followed. Private, loopback, link-local, and metadata destinations remain denied under the public policy. An exact host-approved origin can permit a private service.

Bus operations are provided by the host's tree router. `tree.self` returns the host-assigned namespace and scoped module ID. `interfaces.declare` accepts only `transport_types`, `directions`, `account`, `purpose`, `max_instances`, and `max_interfaces`; the host supplies the module ID. The host operator allocates interfaces through the management API. Calls, event publication, and subscription require a current grant belonging to this module; an interface ID alone supplies no authority. `onebot.receive` is a nonblocking poll returning one observation or null. Guests cannot choose another module's namespace, provide their own module ID, or import host code to construct routes.

## Isolation And Limits

The worker starts using Python isolated mode, a sanitized process environment, and only the host-discovered Wasmtime dependency directory. The guest receives empty WASI arguments and environment, no inherited input/output streams, no preopened directories, and no sockets. Safe clock and random imports are available. Direct WASI filesystem, descriptor, socket, and process-control imports terminate the worker when invoked; granted file and network access uses the broker instead.

The host sets a bounded linear-memory limit, table limit, fuel budget, epoch interruption deadline, and wall deadline. The defaults are 32 MiB linear memory, 5,000,000 fuel per call, a 3-second call deadline, and a 10-second startup deadline. Host policy may choose smaller or bounded larger values.

Permission violations, memory faults, fuel exhaustion, invalid imports, invalid ABI, and timeouts terminate the worker. `status()` reports `wasmtime-process`, the real PID, process state, and a fixed code such as `AEGIS_PERMISSION_DENIED`, `AEGIS_FUEL_EXHAUSTED`, or `AEGIS_TIMEOUT`. Explicit stop kills and reaps the worker and prevents late startup from reviving it.

A broker operation already started in the host may have an unknown result if its module times out. Aegis stops waiting and kills the guest; it does not claim to cancel a completed write or remote request, and it never retries that operation. Host broker implementations must bound their own I/O and release resources on close.

## Gestalt Reference

The current Gestalt `src/aegis.js` supplies a read-only startup assessment, fixed finding codes, required-startup refusals, and a monotonic security floor. Heredity adopts its explicit reporting and refusal principles. Gestalt's current Aegis is not a plugin syscall sandbox or a process-kill implementation. This runtime was implemented separately; no Gestalt source was copied.
