# JoyRead sealed file format (`.jrl`), version 1

This is the normative description of the files JoyRead writes for books and
covers in Hidden Space. The reference implementation is
`src/joyread/core/vault/container.py`, and
`tests/support/vault_golden.py` holds a version-1 file that every build must
keep reading.

## Goals

- **Confidentiality and integrity** of the content, using only published
  constructions.
- **Sealing without a secret.** Writing a file needs only the vault's public
  key, so hiding a book never asks for the passphrase.
- **Random access.** Reading any byte range decrypts only the chunks that
  cover it, and concurrent readers need no shared state.
- **Little metadata.** The plaintext header says nothing about the content.
  The file size reveals the content size only to within the padding (see
  [Padding](#padding)).

Non-goals: hiding that a file is a `.jrl`, hiding which vault sealed it (the
key id is visible), and deniability.

## Notation

All integers are unsigned and big-endian. `||` is concatenation.
`SHA-256` is FIPS 180-4. `HKDF` is RFC 5869 with SHA-256.
`ChaCha20-Poly1305` is the RFC 8439 AEAD with a 12-byte nonce and a 16-byte tag.

## File layout

```
header (104 bytes) || payload
```

### Header

| Offset | Size | Field | Value |
|---:|---:|---|---|
| 0 | 4 | magic | ASCII `JRL1` |
| 4 | 1 | suite | `1` |
| 5 | 1 | chunk_log2 | `16` by default; readers accept 10–24 |
| 6 | 1 | flags | `0` |
| 7 | 1 | reserved | `0` |
| 8 | 16 | key_id | First 16 bytes of `SHA-256("joyread-vault-key-id-v1" \|\| vault_public_key)` |
| 24 | 16 | file_nonce | Random, unique per file |
| 40 | 32 | enc | HPKE encapsulated key |
| 72 | 32 | sealed_file_key | HPKE ciphertext of the 16-byte file key, including its tag |

**Suite 1** is:
- HPKE base mode (RFC 9180) with DHKEM(X25519, HKDF-SHA256) (KEM id `0x0020`), HKDF-SHA256 (KDF `0x0001`) and ChaCha20-Poly1305 (AEAD `0x0003`)
- ChaCha20-Poly1305 for the payload

A reader **must** refuse:
- an unknown suite
- a chunk_log2 outside 10–24
- any nonzero flags or reserved byte

Unknown flags could change how the payload is read, so a reader must not guess.

### File key

The writer draws a random 16-byte `file_key` and seals it to the vault public key:

```
info = "joyread-jrl-v1"
aad  = header[0:40]                 # magic .. file_nonce
enc, sealed_file_key = HPKE.SealBase(vault_public_key, info, aad, file_key)
```

The AAD binds magic, suite, chunk size, flags, key id and nonce to the file key.

### Payload key

```
payload_key = HKDF(ikm  = file_key,
                   salt = file_nonce,
                   info = "joyread-jrl-v1 payload" || SHA-256(header[0:104]),
                   L    = 32)
```

Hashing the whole header into the derivation means no header byte, `enc`
included, can be replaced without the payload failing to decrypt.

### Payload

The plaintext `P` (see below) is split into chunks of `C = 2^chunk_log2`
bytes. Every chunk is exactly `C` bytes except the last, which holds 1 to `C`
bytes. There are no empty chunks. Chunk `i`, counting from 0, is sealed as:

```
nonce_i = i (11 bytes) || last_i (1 byte: 0x01 for the final chunk, else 0x00)
chunk_i = ChaCha20-Poly1305.Seal(payload_key, nonce_i, aad = "", P[i*C : (i+1)*C])
payload = chunk_0 || chunk_1 || ...
```

This is the STREAM construction (Hoang, Reyhanitabar, Rogaway and Vizár), as
used by age.

- **Truncation is detected.** The flag in the nonce means a file cut at a chunk boundary fails, because its new last chunk was sealed as not-last.
- **Reordered or duplicated chunks fail,** because the counter is part of the nonce.

### Plaintext and trailer

```
P = content || zero padding || trailer (32 bytes)
```

| Trailer offset | Size | Field | Value |
|---:|---:|---|---|
| 0 | 4 | magic | ASCII `JRLT` |
| 4 | 1 | trailer_version | `1` |
| 5 | 1 | content_type | `1` CBZ, `2` PDF, `3` PNG |
| 6 | 2 | reserved | `0` |
| 8 | 8 | content_length | Length of `content` in bytes |
| 16 | 16 | reserved | all `0` |

The trailer comes last so a writer can stream content of unknown length.

A reader **must** refuse:
- a wrong trailer magic or an unknown version
- nonzero reserved bytes
- an unknown content type
- `content_length > len(P) - 32`

### Padding

`len(P)` is the Padmé length of `len(content) + 32` (Nikitin et al., PETS 2019):

```
padme(L) = L                          if L < 2
E = floor(log2 L);  S = floor(log2 E) + 1
mask = 2^(E - S) - 1
padme(L) = (L + mask) & ~mask
```

- **What it leaks:** O(log log L) bits of the length.
- **What it costs:** at most about 12% for small files, and under 2% for book-sized ones.

Readers derive `len(P)` from the file size and do not require Padmé, so a
future writer may choose another padding within version 1.

## Reading

1. Parse the header and apply the refusal rules above.
2. If `key_id` does not match the vault, the file belongs to another vault.
3. Open the file key with HPKE. Since the key id matched, failure here means the header is damaged.
4. Derive `payload_key`.
5. From the file size, compute:
   - `body = size - 104`
   - `n = ceil(body / (C + 16))`
   - `len(P) = body - 16n`
   - the final chunk must hold more than its 16-byte tag
6. Decrypt the final chunk(s) to read the trailer.
7. Read content bytes `[a, b)` by decrypting chunks `floor(a/C)` through `floor((b-1)/C)` and slicing. Never return padding or trailer bytes as content.

Any authentication failure fails that read. Content must never be returned
from a chunk that did not authenticate.

## The vault key (informative)

The vault key pair is not stored in `.jrl` files. JoyRead keeps the X25519
public key and the wrapped private key in the Library database, so the vault
moves with the Library:

- **Passphrase slot:** the private key wrapped with ChaCha20-Poly1305 under `scrypt(NFC(passphrase), N=2^17, r=8, p=1)`.
- **Recovery slot:** wrapped under `HKDF` of a 128-bit recovery code.
- **Slot binding:** each wrap's associated data binds the vault, the slot and its parameters.
- **Forward compatibility:** readers skip slot types they do not know, so new unlock methods (such as an OS-keychain PIN) can be added without changing any sealed file.

## Versioning

- A change that existing readers would misread needs a new magic (`JRL2`).
  Examples: a different chunk layout, or a trailer that existing readers would misparse.
- Changes that version-1 readers safely refuse can use a new suite id or flag bit. Example: a new cipher suite.
- Writers must not change the meaning of any version-1 field.
