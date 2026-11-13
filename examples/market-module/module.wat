(module
  (memory (export "memory") 1)
  (global $initialized (mut i32) (i32.const 0))
  (data (i32.const 128) "{\"api_major\":1,\"api_minor\":0,\"module_id\":\"nyxion.example.placeholder\"}")
  (data (i32.const 256) "{\"status\":\"ok\",\"module_id\":\"nyxion.example.placeholder\",\"version\":\"1.0.0\",\"example\":true,\"message\":\"Aegis example is running.\"}")
  (func (export "aegis_alloc") (param i32) (result i32)
    (i32.const 1024))
  (func (export "aegis_call")
    (param $pointer i32) (param $count i32) (result i64)
    (if (result i64) (i32.eqz (global.get $initialized))
      (then
        (global.set $initialized (i32.const 1))
        (i64.or (i64.shl (i64.const 128) (i64.const 32)) (i64.const 70)))
      (else
        (i64.or (i64.shl (i64.const 256) (i64.const 32)) (i64.const 127)))))
)
