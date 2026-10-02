"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Check, FileUp, LoaderCircle, Mail, Play, RotateCcw, Settings2, Square, UserRoundPlus } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import {
  createRegistrationJob, fetchRegistrationJobs, fetchRegistrationSettings, previewRegistrationEmails,
  retryRegistrationJob, saveRegistrationSettings, stopRegistrationJob, testRegistrationMailbox,
  registrationManualAction, fetchRegistrationDiagnosticImage,
  testRegistrationRoxy, type RoxyWorkspace,
  type EmailPreview, type RegistrationJob, type RegistrationSettings,
} from "@/lib/registration-api";

const activeStatuses = new Set(["queued", "running", "stopping", "waiting_manual"]);
const labels: Record<string, string> = {
  queued: "等待中", running: "进行中", stopping: "停止中", success: "已入池", failed: "失败",
  manual_required: "需人工处理", interrupted: "已中断", cancelled: "已停止", skipped: "已跳过",
  stopped: "已停止", completed: "已完成",
  waiting_manual: "等待人工验证",
};
const emptySettings: RegistrationSettings = {
  site_url: "https://ccmtc.cfd/mail", email: "", client_id: "", protocol: "imap", configured: false,
  has_mailbox_password: false, has_registration_password: false,
};
function message(error: unknown) { return error instanceof Error ? error.message : "操作失败，请重试"; }

export function AutoRegistrationDialog({ disabled, onImported }: { disabled?: boolean; onImported: () => void }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<"emails" | "settings" | "progress">("emails");
  const [settings, setSettings] = useState(emptySettings);
  const [ready, setReady] = useState(false);
  const [credentials, setCredentials] = useState("");
  const [password, setPassword] = useState("");
  const [protocol, setProtocol] = useState<"imap" | "graph">("imap");
  const [driver, setDriver] = useState<"chromium" | "roxy">("chromium");
  const [roxyBase, setRoxyBase] = useState("http://127.0.0.1:50000");
  const [roxyToken, setRoxyToken] = useState("");
  const [roxyWorkspace, setRoxyWorkspace] = useState("");
  const [roxyProject, setRoxyProject] = useState("");
  const [roxyItems, setRoxyItems] = useState<RoxyWorkspace[]>([]);
  const [roxyResult, setRoxyResult] = useState("");
  const [content, setContent] = useState("");
  const [preview, setPreview] = useState<EmailPreview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [jobs, setJobs] = useState<RegistrationJob[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [busy, setBusy] = useState("");
  const [mailResult, setMailResult] = useState("");
  const [loadError, setLoadError] = useState("");
  const [diagnosticImage, setDiagnosticImage] = useState("");
  useEffect(() => () => { if (diagnosticImage) URL.revokeObjectURL(diagnosticImage); }, [diagnosticImage]);
  const fileRef = useRef<HTMLInputElement>(null);
  const importedCounts = useRef(new Map<string, number>());
  const onImportedRef = useRef(onImported);
  onImportedRef.current = onImported;
  const job = jobs.find((item) => item.id === selectedId) ?? jobs[0];
  const active = jobs.some((item) => activeStatuses.has(item.status));
  const browserConfigured = settings.registration_driver !== "roxy" || (!!settings.has_roxy_api_token && !!settings.roxy_workspace_id);
  const loadBrowserSettings = (saved: RegistrationSettings) => {
    setDriver(saved.registration_driver ?? "chromium"); setRoxyBase(saved.roxy_api_base ?? "http://127.0.0.1:50000");
    setRoxyWorkspace(saved.roxy_workspace_id ?? ""); setRoxyProject(saved.roxy_project_id ?? "");
  };

  const refresh = useCallback(async () => {
    const response = await fetchRegistrationJobs();
    let changed = false;
    for (const item of response.items) {
      const count = item.rows.filter((row) => row.imported && row.status !== "skipped").length;
      const previous = importedCounts.current.get(item.id);
      if (previous !== undefined && count > previous) changed = true;
      importedCounts.current.set(item.id, count);
    }
    setJobs(response.items);
    setLoadError("");
    if (changed) onImportedRef.current();
  }, []);

  useEffect(() => {
    let alive = true;
    Promise.all([fetchRegistrationSettings(), fetchRegistrationJobs()]).then(([saved, result]) => {
      if (!alive) return;
      setSettings(saved); setProtocol(saved.protocol); setJobs(result.items); setReady(true);
      loadBrowserSettings(saved);
      for (const item of result.items) importedCounts.current.set(item.id, item.rows.filter((row) => row.imported).length);
    }).catch((error) => { if (alive) setLoadError(message(error)); });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    if (!open && !active) return;
    let disposed = false, pending = false;
    const poll = async () => {
      if (pending) return;
      pending = true;
      try { if (!disposed) await refresh(); }
      catch (error) { if (!disposed) setLoadError(message(error)); }
      finally { pending = false; }
    };
    void poll();
    const timer = window.setInterval(poll, 3000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [open, active, refresh]);

  useEffect(() => {
    setPreview(null); setPreviewError("");
    if (!content.trim()) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      previewRegistrationEmails(content).then((result) => { if (!cancelled) setPreview(result); })
        .catch((error) => { if (!cancelled) setPreviewError(message(error)); });
    }, 300);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [content]);

  const operate = async (name: string, action: () => Promise<void>) => {
    setBusy(name);
    try { await action(); } catch (error) { toast.error(message(error)); } finally { setBusy(""); }
  };
  const updateJob = (next: RegistrationJob) => {
    setJobs((current) => [next, ...current.filter((item) => item.id !== next.id)]);
    setSelectedId(next.id); setTab("progress");
    if (!importedCounts.current.has(next.id)) importedCounts.current.set(next.id, 0);
  };
  const save = () => operate("save", async () => {
    const saved = await saveRegistrationSettings({ credential_line: credentials, protocol, registration_driver: driver, roxy_api_base: roxyBase,
      roxy_workspace_id: roxyWorkspace, roxy_project_id: roxyProject, ...(roxyToken ? {roxy_api_token: roxyToken} : {}), ...(password ? { registration_password: password } : {}) });
    setSettings(saved); setCredentials(""); setPassword(""); setRoxyToken(""); setMailResult(""); setRoxyResult(""); toast.success("注册设置已保存");
  });
  const test = () => operate("test", async () => {
    const result = await testRegistrationMailbox(); setMailResult(result.message); toast.success(result.message);
  });
  const start = () => operate("start", async () => {
    updateJob(await createRegistrationJob(content)); toast.success("任务已启动，关闭弹窗后仍会继续");
  });

  return <>
    <Button variant="outline" className="h-10 rounded-xl border-stone-200 bg-white/80 px-4 text-stone-700" disabled={disabled}
      onClick={() => { setTab(active ? "progress" : "emails"); setOpen(true); }}>
      {active ? <LoaderCircle className="size-4 animate-spin" /> : <UserRoundPlus className="size-4" />}
      {active ? "注册进行中" : "自动注册"}
    </Button>
    <Dialog open={open} onOpenChange={(next) => { setOpen(next); if (!next) { setCredentials(""); setPassword(""); setRoxyToken(""); } }}>
      <DialogContent className="flex max-h-[90vh] w-[min(96vw,1080px)] flex-col gap-5 overflow-hidden">
        <DialogHeader>
          <DialogTitle>批量自动注册</DialogTitle>
          <DialogDescription>导入域名邮箱，通过统一收件邮箱接码，完成 OAuth 授权后自动加入号池。</DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-3 gap-2" role="tablist" aria-label="自动注册步骤">
          {([
            ["emails", "批量邮箱", Mail], ["settings", "接码设置", Settings2], ["progress", "任务进度", LoaderCircle],
          ] as const).map(([key, label, Icon], index) => <button key={key} role="tab" aria-selected={tab === key} aria-controls={`registration-panel-${key}`} id={`registration-tab-${key}`}
            className={cn("flex items-center justify-center gap-2 rounded-xl border px-2 py-3 text-sm", tab === key ? "border-stone-900 bg-stone-900 text-white" : "border-stone-200 text-stone-600 hover:bg-stone-50")}
            onClick={() => setTab(key)}><Icon className="size-4" /><span>{index + 1}. {label}</span></button>)}
        </div>
        {loadError && <div className="flex items-center justify-between rounded-xl bg-rose-50 p-3 text-sm text-rose-700">{loadError}<Button variant="ghost" size="sm" onClick={() => void operate("reload", async () => {
          const saved = await fetchRegistrationSettings(); setSettings(saved); setProtocol(saved.protocol); loadBrowserSettings(saved); setReady(true); await refresh();
        })}>重新加载</Button></div>}
        <div className="min-h-0 flex-1 overflow-y-auto" role="tabpanel" id={`registration-panel-${tab}`} aria-labelledby={`registration-tab-${tab}`}>
          {tab === "emails" && <div className="space-y-4">
            <p className="text-xs text-stone-500">注册浏览器：{settings.registration_driver === "roxy" ? "RoxyBrowser（每账号独立环境）" : "本地 Chromium"}{!browserConfigured && " · 尚未配置，请在接码设置中填写 API Token 并选择工作区"}</p>
            <p className="rounded-xl bg-amber-50 p-3 text-sm text-amber-800">{settings.manual_available ? "遇到安全验证时会保留本机浏览器窗口，暂停整个批次。完成验证后，在任务进度中点击继续。" : "当前服务器没有可交互桌面。安全验证需在服务器配置 DISPLAY 与远程桌面，或在本机运行服务后重试；此页面无法远程操控无头浏览器。"}</p>
            <div className="flex items-center justify-between rounded-xl bg-stone-50 p-3 text-sm">
              <span>{settings.configured ? `接码邮箱：${settings.email} · ${settings.protocol.toUpperCase()}` : "先配置统一接码邮箱，再开始注册"}</span>
              <Button variant="ghost" size="sm" onClick={() => setTab("settings")}>{settings.configured ? "修改设置" : "配置接码"}</Button>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <label htmlFor="registration-emails" className="text-sm font-medium">待注册邮箱</label>
              <Button variant="outline" size="sm" onClick={() => fileRef.current?.click()}><FileUp className="size-4" />导入 TXT / JSON</Button>
              <input ref={fileRef} type="file" accept=".txt,.json,text/plain,application/json" className="hidden" aria-label="导入邮箱文件" onChange={(event) => {
                const file = event.target.files?.[0]; event.target.value = "";
                if (!file) return;
                if (file.size > 200000) { toast.error("文件最多 200KB"); return; }
                file.text().then(setContent).catch(() => toast.error("读取文件失败"));
              }} />
            </div>
            <Textarea id="registration-emails" className="min-h-44 font-mono text-sm" value={content} onChange={(event) => setContent(event.target.value)}
              placeholder={'每行一个邮箱，例如：\nalice@example.com\nbob@example.com\n\n也支持 JSON：["alice@example.com", "bob@example.com"]'} />
            <p className="text-xs text-stone-500">每批最多 500 个。请确保邮件会转发到接码邮箱；重复项自动去重，已在号池中的邮箱跳过。</p>
            {previewError && <p className="text-sm text-rose-600">{previewError}</p>}
            {preview && <div className="space-y-2 rounded-xl border p-3 text-sm">
              <div className="flex gap-5"><span className="text-emerald-700">有效 {preview.emails.length}</span><span className="text-stone-500">重复 {preview.duplicates.length}</span><span className={preview.errors.length ? "text-rose-600" : "text-stone-500"}>错误 {preview.errors.length}</span></div>
              <div className="max-h-32 overflow-y-auto">
                {preview.errors.map((item, index) => <p key={index} className="text-rose-600">{item.line ? `第 ${item.line} 行：` : ""}{item.error}</p>)}
                {preview.duplicates.map((item) => <p key={item.line} className="text-stone-500">第 {item.line} 行重复：{item.email}</p>)}
                {!preview.errors.length && <p className="break-all text-stone-500">{preview.emails.slice(0, 8).join("、")}{preview.emails.length > 8 ? "…" : ""}</p>}
              </div>
            </div>}
            <div className="flex items-center justify-between gap-3 border-t pt-4">
              <p className="text-xs text-stone-500">逐个注册；安全验证时暂停等待人工处理，无法继续的账号记录原因后处理下一项。</p>
              <Button disabled={!ready || !settings.configured || !browserConfigured || !preview?.emails.length || !!preview?.errors.length || active || !!busy} onClick={() => void start()}>
                {busy === "start" ? <LoaderCircle className="size-4 animate-spin" /> : <Play className="size-4" />}开始注册
              </Button>
            </div>
          </div>}
          {tab === "settings" && <div className="space-y-4">
            <div className="space-y-3 rounded-xl border p-4">
              <h3 className="text-sm font-medium">注册浏览器</h3>
              <select aria-label="注册浏览器驱动" className="h-10 w-full rounded-xl border bg-white px-3 text-sm" value={driver} disabled={active} onChange={(event) => setDriver(event.target.value as "chromium" | "roxy")}>
                <option value="chromium">本地 Chromium</option><option value="roxy">RoxyBrowser（本地 API + 可见窗口）</option>
              </select>
              {driver === "roxy" && <>
                <p className="text-xs text-stone-500">先安装并登录 RoxyBrowser，在「API → API 配置」启用 API。每个账号创建独立环境，结束后关闭并删除本次创建的环境。安全验证由你在原窗口完成。</p>
                <div className="grid gap-3 sm:grid-cols-2">
                  <div><label htmlFor="roxy-api-base" className="text-xs">本机 API 地址</label><Input id="roxy-api-base" value={roxyBase} onChange={(event) => {setRoxyBase(event.target.value); setRoxyItems([]);}} disabled={active} /></div>
                  <div><label htmlFor="roxy-api-token" className="text-xs">API Token（仅后端保存）</label><Input id="roxy-api-token" type="password" autoComplete="new-password" value={roxyToken} onChange={(event) => setRoxyToken(event.target.value)} placeholder={settings.has_roxy_api_token ? "已保存，留空保持" : "填写 Roxy API Key"} disabled={active} /></div>
                  <div><label htmlFor="roxy-workspace" className="text-xs">工作区 ID</label><Input id="roxy-workspace" value={roxyWorkspace} onChange={(event) => setRoxyWorkspace(event.target.value)} disabled={active} /></div>
                  <div><label htmlFor="roxy-project" className="text-xs">项目 ID（API 要求时填写）</label><Input id="roxy-project" value={roxyProject} onChange={(event) => setRoxyProject(event.target.value)} disabled={active} /></div>
                </div>
                <Button variant="outline" size="sm" disabled={active || !!busy || !settings.has_roxy_api_token || !!roxyToken || roxyBase !== settings.roxy_api_base} onClick={() => void operate("test-roxy", async () => { const response = await testRegistrationRoxy(); setRoxyItems(response.items); setRoxyResult(response.message); })}>测试连接 / 读取工作区</Button>
                {roxyResult && <p className="text-xs text-emerald-700">{roxyResult}</p>}
                {roxyItems.length > 0 && <select aria-label="选择 Roxy 工作区及项目" value={`${roxyWorkspace}:${roxyProject}`} className="h-10 w-full rounded-xl border bg-white px-3 text-sm" disabled={active} onChange={(event) => {const item = roxyItems.find((item) => `${item.workspace_id}:${item.project_id}` === event.target.value); if (item) {setRoxyWorkspace(item.workspace_id); setRoxyProject(item.project_id);}}}><option value=":">请选择工作区及项目</option>{roxyItems.map((item) => <option key={`${item.workspace_id}:${item.project_id}`} value={`${item.workspace_id}:${item.project_id}`}>{item.workspace_name} / {item.project_name || "未分项目"}</option>)}</select>}
                <p className="text-xs text-stone-500">修改后先保存，再测试连接；选好工作区后再次保存。</p>
              </>}
            </div>
            <div className="rounded-xl bg-stone-50 p-3 text-sm text-stone-600">取件网站：<a href={settings.site_url} target="_blank" rel="noreferrer" className="underline">ccmtc 邮箱取件</a> · 收件箱</div>
            <div className="space-y-2"><label htmlFor="registration-credential" className="text-sm font-medium">统一收件邮箱凭据</label>
              <Textarea id="registration-credential" value={credentials} onChange={(event) => setCredentials(event.target.value)} className="min-h-24 font-mono text-sm" autoComplete="off"
                placeholder={settings.configured ? "已保存凭据。留空保持不变；粘贴新凭据可替换。" : "邮箱----密码----client_id----refresh_token"} />
              <p className="text-xs text-stone-500">仅在服务器保存，不回显密码和 token，也不写入任务日志。</p>
              {settings.configured && <p className="text-sm text-emerald-700">已配置：{settings.email}</p>}
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2"><label htmlFor="registration-protocol" className="text-sm font-medium">取件协议</label>
                <select id="registration-protocol" className="h-10 w-full rounded-xl border bg-white px-3 text-sm" value={protocol} onChange={(event) => setProtocol(event.target.value as "imap" | "graph")} disabled={active}>
                  <option value="imap">IMAP（默认）</option><option value="graph">Graph</option>
                </select></div>
              <div className="space-y-2"><label htmlFor="registration-password" className="text-sm font-medium">注册密码（流程要求时使用）</label>
                <Input id="registration-password" type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder={settings.has_registration_password ? "已保存，留空保持" : "输入你希望统一使用的密码"} disabled={active} /></div>
            </div>
            <p className="text-xs text-stone-500">姓名自动生成英文名；年龄随机为 26–60 岁。失败重试沿用原资料。</p>
            {mailResult && <p role="status" className="rounded-xl bg-emerald-50 p-3 text-sm text-emerald-700">{mailResult}</p>}
            <div className="flex flex-wrap gap-2 border-t pt-4">
              <Button onClick={() => void save()} disabled={active || !!busy || !ready}>{busy === "save" && <LoaderCircle className="size-4 animate-spin" />}保存接码设置</Button>
              <Button variant="outline" onClick={() => void test()} disabled={active || !!busy || !settings.configured || !!credentials || !!password || protocol !== settings.protocol}>{busy === "test" && <LoaderCircle className="size-4 animate-spin" />}测试取件</Button>
              <Button variant="ghost" onClick={() => setTab("emails")}>返回邮箱列表</Button>
            </div>
            <p className="text-xs text-stone-500">修改后请先保存，再测试取件。任务运行期间不能更换设置。</p>
          </div>}
          {tab === "progress" && <div className="space-y-4">
            {!job ? <div className="py-16 text-center text-sm text-stone-500">还没有注册任务。<button className="ml-2 underline" onClick={() => setTab("emails")}>导入邮箱开始</button></div> : <>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <select aria-label="选择注册任务" value={job.id} onChange={(event) => setSelectedId(event.target.value)} className="max-w-full rounded-xl border bg-white px-3 py-2 text-sm">
                  {jobs.map((item) => <option key={item.id} value={item.id}>{new Date(item.created_at * 1000).toLocaleString()} · {item.total} 个邮箱 · {labels[item.status] ?? item.status}</option>)}
                </select>
                <span className="text-sm text-stone-500">{labels[job.status] ?? job.status} · {job.status === "waiting_manual" ? "整个批次已暂停，原浏览器会话保留" : "关闭弹窗后任务保留"}</span>
              </div>
              <div className="grid grid-cols-4 gap-2">{[["总数", job.total], ["成功", job.success], ["失败 / 中断", job.failed], ["剩余", job.remaining]].map(([label, value]) => <div key={label} className="rounded-xl bg-stone-50 p-3"><div className="text-xs text-stone-500">{label}</div><div className="mt-1 text-xl font-semibold">{value}</div></div>)}</div>
              <div className="max-h-[42vh] overflow-auto rounded-xl border">
                <table className="w-full text-left text-sm"><thead className="sticky top-0 bg-stone-50 text-xs text-stone-500"><tr><th className="p-3">邮箱 / 资料</th><th className="p-3">状态</th><th className="p-3">进度 / 原因</th></tr></thead>
                  <tbody>{job.rows.map((row) => <tr key={row.id} className="border-t align-top"><td className="p-3"><div className="break-all font-medium">{row.email}</div><div className="mt-1 text-xs text-stone-400">{row.name} · {row.age} 岁</div></td>
                    <td className={cn("whitespace-nowrap p-3", row.status === "success" ? "text-emerald-700" : row.error ? "text-rose-600" : "text-stone-500")}>{row.status === "running" && <LoaderCircle className="mr-1 inline size-3 animate-spin" />}{labels[row.status] ?? row.status}</td>
                    <td className="min-w-48 p-3"><p>{row.message}</p>{row.warning && <p className="mt-1 text-xs text-amber-700">{row.warning}</p>}
                      {row.status === "waiting_manual" && <div className="mt-3 space-y-2 rounded-lg bg-amber-50 p-3">
                        <p className="text-xs text-amber-800">在运行服务的电脑上完成 Chromium 安全验证。{row.manual_expires_at && `会话保留至 ${new Date(row.manual_expires_at * 1000).toLocaleTimeString()}。`}等待期间不注册其他邮箱。</p>
                        <div className="flex flex-wrap gap-2">{([["show", "显示验证窗口"], ["continue", "已完成验证，继续"], ["cancel", "取消此账号"]] as const).map(([action, label]) => <Button key={action} variant={action === "continue" ? "default" : "outline"} size="sm" disabled={!!busy} onClick={() => void operate(action, async () => { updateJob(await registrationManualAction(job.id, row.id, action)); })}>{label}</Button>)}</div>
                      </div>}
                      {row.diagnostics && <details className="mt-2 text-xs text-stone-500"><summary className="cursor-pointer">查看阻断诊断</summary><p className="mt-2 break-all">{row.diagnostics.title} · {row.diagnostics.url}</p><p>{new Date(row.diagnostics.time * 1000).toLocaleString()}</p>{row.diagnostics.failures.map((item, index) => <p key={index} className="break-all">{item.status} · {item.url}</p>)}{row.diagnostics.screenshot && <Button size="sm" variant="ghost" disabled={!!busy} onClick={() => void operate("diagnostic", async () => setDiagnosticImage(await fetchRegistrationDiagnosticImage(job.id, row.id)))}>查看脱敏截图</Button>}</details>}
                      <div className="mt-2 flex gap-3 text-xs">{([["注册", row.registered], ["授权", row.authorized], ["入池", row.imported]] as const).map(([label, done]) => <span key={label} className={done ? "text-emerald-600" : "text-stone-300"}>{done && <Check className="mr-1 inline size-3" />}{label}</span>)}</div></td></tr>)}</tbody>
                </table>
              </div>
              {diagnosticImage && <div className="rounded-xl border p-3"><Button variant="ghost" size="sm" onClick={() => setDiagnosticImage("")}>关闭截图</Button>{/* eslint-disable-next-line @next/next/no-img-element */}<img src={diagnosticImage} alt="注册阻断的脱敏截图" className="w-full rounded-lg" /></div>}
              <div className="flex flex-wrap gap-2 border-t pt-4">
                {activeStatuses.has(job.status) ? <Button variant="outline" disabled={!!busy || job.status === "stopping"} onClick={() => void operate("stop", async () => { updateJob(await stopRegistrationJob(job.id)); })}><Square className="size-4" />{job.status === "stopping" ? "正在停止" : "停止任务"}</Button>
                  : <Button variant="outline" disabled={!!busy || active || job.failed === 0} onClick={() => void operate("retry", async () => { updateJob(await retryRegistrationJob(job.id)); })}><RotateCcw className="size-4" />重试失败项</Button>}
                <Button variant="ghost" onClick={() => setTab("emails")}>新建批次</Button>
              </div>
            </>}
          </div>}
        </div>
      </DialogContent>
    </Dialog>
  </>;
}
