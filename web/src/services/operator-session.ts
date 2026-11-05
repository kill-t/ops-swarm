import { readonly, shallowRef } from "vue";

export interface OperatorIdentity {
  credential_id: string;
  subject_id: string;
  role: string;
  permissions: string[];
  expires_at: string;
}

const identity = shallowRef<OperatorIdentity | null>(null);
let authorization: string | undefined;
let expiryTimer: ReturnType<typeof setTimeout> | undefined;
export const operatorIdentity = readonly(identity);

export function operatorAuthorization(): string | undefined {
  return authorization;
}

export function setOperatorSession(token: string, principal: OperatorIdentity): void {
  clearTimeout(expiryTimer);
  authorization = `Bearer ${token}`;
  identity.value = principal;
  const expiresAt = Date.parse(principal.expires_at);
  function expire(): void {
    const remaining = expiresAt - Date.now();
    if (remaining > 0) {
      expiryTimer = setTimeout(expire, Math.min(remaining, 2_147_483_647));
    } else {
      clearOperatorSession();
    }
  }
  expire();
}

export function clearOperatorSession(): void {
  clearTimeout(expiryTimer);
  authorization = undefined;
  identity.value = null;
}
