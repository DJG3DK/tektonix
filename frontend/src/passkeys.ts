/* The browser half of passkeys: turning the server's JSON options into what
   navigator.credentials wants, and the credential back into JSON. Newer
   browsers do both themselves (parse*FromJSON, toJSON); the manual path is
   for the ones that do not, including the Windows app's WebView on older
   builds. Server side: agent/passkeys.py. */

type Json = Record<string, unknown>;

export function passkeysSupported(): boolean {
  return typeof window !== "undefined" && typeof window.PublicKeyCredential === "function"
    && !!navigator.credentials;
}

export function fromBase64url(s: string): ArrayBuffer {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

export function toBase64url(buf: ArrayBuffer | ArrayBufferView): string {
  const bytes = buf instanceof ArrayBuffer ? new Uint8Array(buf) : new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

type PKC = typeof PublicKeyCredential & {
  parseCreationOptionsFromJSON?: (o: Json) => PublicKeyCredentialCreationOptions;
  parseRequestOptionsFromJSON?: (o: Json) => PublicKeyCredentialRequestOptions;
};

function descriptors(list: unknown): PublicKeyCredentialDescriptor[] | undefined {
  if (!Array.isArray(list)) return undefined;
  return list.map((d: Json) => ({ ...(d as object), id: fromBase64url(String(d.id)) }) as PublicKeyCredentialDescriptor);
}

export function creationOptions(json: Json): PublicKeyCredentialCreationOptions {
  const pkc = window.PublicKeyCredential as PKC | undefined;
  if (pkc?.parseCreationOptionsFromJSON) return pkc.parseCreationOptionsFromJSON(json);
  const user = json.user as Json;
  return {
    ...(json as object),
    challenge: fromBase64url(String(json.challenge)),
    user: { ...(user as object), id: fromBase64url(String(user.id)) },
    excludeCredentials: descriptors(json.excludeCredentials),
  } as PublicKeyCredentialCreationOptions;
}

export function requestOptions(json: Json): PublicKeyCredentialRequestOptions {
  const pkc = window.PublicKeyCredential as PKC | undefined;
  if (pkc?.parseRequestOptionsFromJSON) return pkc.parseRequestOptionsFromJSON(json);
  return {
    ...(json as object),
    challenge: fromBase64url(String(json.challenge)),
    allowCredentials: descriptors(json.allowCredentials),
  } as PublicKeyCredentialRequestOptions;
}

export function credentialJSON(cred: PublicKeyCredential): Json {
  const withJSON = cred as PublicKeyCredential & { toJSON?: () => Json };
  if (typeof withJSON.toJSON === "function") return withJSON.toJSON() as unknown as Json;
  const r = cred.response as AuthenticatorAttestationResponse & AuthenticatorAssertionResponse;
  const response: Json = { clientDataJSON: toBase64url(r.clientDataJSON) };
  if ("attestationObject" in r && r.attestationObject) {
    response.attestationObject = toBase64url(r.attestationObject);
    response.transports = typeof r.getTransports === "function" ? r.getTransports() : [];
  } else {
    response.authenticatorData = toBase64url(r.authenticatorData);
    response.signature = toBase64url(r.signature);
    if (r.userHandle) response.userHandle = toBase64url(r.userHandle);
  }
  return {
    id: cred.id, rawId: toBase64url(cred.rawId), type: cred.type, response,
    clientExtensionResults: cred.getClientExtensionResults?.() ?? {},
    authenticatorAttachment: (cred as PublicKeyCredential & { authenticatorAttachment?: string }).authenticatorAttachment,
  };
}

/* What a person reads when the browser's own prompt fails. The common case,
   NotAllowedError, is "they cancelled or it timed out", not an error. */
export function passkeyErrorText(err: unknown, fallback: string): string {
  const name = err instanceof DOMException ? err.name : "";
  if (name === "NotAllowedError") return "The passkey prompt was cancelled or timed out.";
  if (name === "InvalidStateError") return "This device already has a passkey for your account.";
  if (name === "SecurityError") return "Passkeys can't be used on this address.";
  return err instanceof Error ? err.message : fallback;
}
