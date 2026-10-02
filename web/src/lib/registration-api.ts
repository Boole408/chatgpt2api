import { httpRequest, request } from "@/lib/request";

export type RegistrationSettings = {
  site_url: string; email: string; client_id: string; protocol: "imap" | "graph";
  configured: boolean; has_mailbox_password: boolean; has_registration_password: boolean;
  manual_available?: boolean;
  registration_driver?: "chromium" | "roxy"; roxy_api_base?: string; roxy_workspace_id?: string; roxy_project_id?: string; has_roxy_api_token?: boolean;
};
export type EmailPreview = {
  emails: string[]; duplicates: { line: number; email: string }[];
  errors: { line: number; error: string }[];
};
export type RegistrationRow = {
  id: string; email: string; name: string; age: number; status: string; stage: string;
  message: string; error?: string; warning?: string;
  registered: boolean; authorized: boolean; imported: boolean;
  manual_expires_at?: number;
  diagnostics?: { reason: string; url: string; title: string; time: number; screenshot: boolean; failures: {url: string; status: number | string}[] };
};
export type RegistrationJob = {
  id: string; status: string; created_at: number; rows: RegistrationRow[];
  total: number; success: number; failed: number; remaining: number;
};
const base = "/api/accounts/registration";
export const fetchRegistrationSettings = () => httpRequest<RegistrationSettings>(`${base}/settings`);
export const saveRegistrationSettings = (body: { credential_line: string; protocol: string; registration_password?: string; registration_driver?: string; roxy_api_base?: string; roxy_api_token?: string; roxy_workspace_id?: string; roxy_project_id?: string }) =>
  httpRequest<RegistrationSettings>(`${base}/settings`, { method: "POST", body });
export const testRegistrationMailbox = () => httpRequest<{ ok: boolean; count: number; message: string }>(`${base}/test-mailbox`, { method: "POST" });
export type RoxyWorkspace = {workspace_id: string; workspace_name: string; project_id: string; project_name: string};
export const testRegistrationRoxy = () => httpRequest<{ok: boolean; message: string; items: RoxyWorkspace[]}>(`${base}/test-roxy`, {method: "POST"});
export const previewRegistrationEmails = (content: string) => httpRequest<EmailPreview>(`${base}/preview`, { method: "POST", body: { content } });
export const fetchRegistrationJobs = () => httpRequest<{ items: RegistrationJob[] }>(`${base}/jobs`);
export const createRegistrationJob = (content: string) => httpRequest<RegistrationJob>(`${base}/jobs`, { method: "POST", body: { content } });
export const stopRegistrationJob = (id: string) => httpRequest<RegistrationJob>(`${base}/jobs/${id}/stop`, { method: "POST" });
export const retryRegistrationJob = (id: string) => httpRequest<RegistrationJob>(`${base}/jobs/${id}/retry`, { method: "POST" });
export const registrationManualAction = (id: string, row: string, action: "show" | "continue" | "cancel") =>
  httpRequest<RegistrationJob>(`${base}/jobs/${id}/rows/${row}/manual`, {method: "POST", body: {action}});
export const fetchRegistrationDiagnosticImage = async (id: string, row: string) => {
  const response = await request.get<Blob>(`${base}/jobs/${id}/rows/${row}/diagnostic-image`, {responseType: "blob"});
  return URL.createObjectURL(response.data);
};
