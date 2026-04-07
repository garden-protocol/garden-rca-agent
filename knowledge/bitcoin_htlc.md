# Bitcoin HTLC — Tapscript Leaves

P2TR (Pay-to-Taproot) output with three script leaves in its taptree. **This script is canonical and never changes.**

## Spending Paths

| Path | Witness | Condition |
|---|---|---|
| Redeem | `[preimage, redeemer_sig]` | `SHA256(preimage) == secret_hash` AND redeemer signs |
| Refund | `[initiator_sig]` | `timelock` blocks elapsed (OP_CSV) AND initiator signs |
| Instant Refund | `[redeemer_sig, initiator_sig]` | 2-of-2 multisig, no timelock |

## Script Source

### `redeem_leaf`

```rust
pub fn redeem_leaf(secret_hash: &[u8; 32], redeemer_pubkey: &XOnlyPublicKey) -> ScriptBuf {
    Builder::new()
        .push_opcode(opcodes::all::OP_SHA256)
        .push_slice(secret_hash)
        .push_opcode(opcodes::all::OP_EQUALVERIFY)
        .push_slice(redeemer_pubkey.serialize())
        .push_opcode(opcodes::all::OP_CHECKSIG)
        .into_script()
}
```

### `refund_leaf`

```rust
pub fn refund_leaf(timelock: u64, initiator_pubkey: &XOnlyPublicKey) -> ScriptBuf {
    Builder::new()
        .push_int(timelock as i64)
        .push_opcode(opcodes::all::OP_CSV)
        .push_opcode(opcodes::all::OP_DROP)
        .push_slice(&initiator_pubkey.serialize())
        .push_opcode(opcodes::all::OP_CHECKSIG)
        .into_script()
}
```

### `instant_refund_leaf`

```rust
pub fn instant_refund_leaf(
    initiator_pubkey: &XOnlyPublicKey,
    redeemer_pubkey: &XOnlyPublicKey,
) -> ScriptBuf {
    Builder::new()
        .push_slice(&initiator_pubkey.serialize())
        .push_opcode(opcodes::all::OP_CHECKSIG)
        .push_slice(&redeemer_pubkey.serialize())
        .push_opcode(opcodes::all::OP_CHECKSIGADD)
        .push_int(2)
        .push_opcode(opcodes::all::OP_NUMEQUAL)
        .into_script()
}
```
