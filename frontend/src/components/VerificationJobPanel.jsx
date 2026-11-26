import { useState, useEffect, useRef, useCallback } from 'react';
import { api } from '../api';
import { useNotify } from '../context/NotificationContext';
import { useConfirm } from '../context/ConfirmContext';
import { Button } from './ui/Button';

const ACTIVE = ['queued', 'running'];
const POLL_ACTIVE_MS = 3000;
const POLL_PAUSED_MS = 10000;

const STATUS_BADGE = {
  queued:    { label: 'Queued',    cls: 'bg-gray-100 text-gray-600 border-gray-200' },
  running:   { label: 'Running',   cls: 'bg-teal-50 text-teal-700 border-teal-200' },
  paused:    { label: 'Paused',    cls: 'bg-amber-50 text-amber-700 border-amber-200' },
  completed: { label: 'Completed', cls: 'bg-green-50 text-green-700 border-green-200' },
  cancelled: { label: 'Cancelled', cls: 'bg-gray-100 text-gray-500 border-gray-200' },
};

const RESULT_STYLE = {
  valid:     'text-green-700',
  invalid:   'text-red-600',
  risky:     'text-amber-600',
  catch_all: 'text-blue-600',
  unknown:   'text-gray-500',
};

const dismissKey = campaignId => `verificationJobDismissed:${campaignId}`;

/**
 * Progress and controls for a campaign's latest email verification job.
 * Polls while the job is active and calls `onProgress` whenever more leads
 * have been processed, so the leads table stays current without refreshing.
 */
export default function VerificationJobPanel({ campaignId, reloadKey, onProgress }) {
  const notify = useNotify();
  const confirm = useConfirm();
  const [job, setJob] = useState(null);
  const [busy, setBusy] = useState(null); // action in flight
  const [showFailed, setShowFailed] = useState(false);
  const [failedItems, setFailedItems] = useState([]);
  const [dismissedId, setDismissedId] = useState(() => {
    try { return Number(localStorage.getItem(dismissKey(campaignId))) || null; } catch { return null; }
  });
  const processedRef = useRef(null);
  const statusRef = useRef(null);
  // Keep the latest callback without making `load` (and the polling) depend on it
  const onProgressRef = useRef(onProgress);
  onProgressRef.current = onProgress;

  const load = useCallback(async () => {
    try {
      const jobs = await api.get(`/verification-jobs?campaign_id=${campaignId}&limit=1`);
      const latest = jobs?.[0] || null;
      setJob(latest);
      if (!latest) return;

      const processed = latest.done + latest.failed + latest.cancelled;
      const prevStatus = statusRef.current;
      const statusChanged = prevStatus && prevStatus !== latest.status;
      if (processedRef.current !== null && (processed !== processedRef.current || statusChanged)) {
        onProgressRef.current?.(latest);
      }
      if (prevStatus && ACTIVE.includes(prevStatus) && latest.status === 'completed') {
        notify({
          type: latest.failed ? 'warning' : 'success',
          message: latest.failed
            ? `Verification finished: ${latest.done} checked, ${latest.failed} failed`
            : `Verification finished: ${latest.done} lead(s) checked`,
        });
      }
      processedRef.current = processed;
      statusRef.current = latest.status;
    } catch {
      // transient errors: keep the last known state
    }
  }, [campaignId, notify]);

  useEffect(() => { load(); }, [load, reloadKey]);

  // Poll while there is something to watch
  useEffect(() => {
    if (!job) return undefined;
    const delay = ACTIVE.includes(job.status) ? POLL_ACTIVE_MS
      : job.status === 'paused' ? POLL_PAUSED_MS : null;
    if (!delay) return undefined;
    const t = setTimeout(load, delay);
    return () => clearTimeout(t);
  }, [job, load]);

  const loadFailed = useCallback(async () => {
    if (!job) return;
    try {
      setFailedItems(await api.get(`/verification-jobs/${job.id}/items?status=failed&limit=100`));
    } catch (e) {
      notify({ type: 'error', message: e.message });
    }
  }, [job?.id, notify]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (showFailed) loadFailed();
  }, [showFailed, job?.failed, loadFailed]);

  const act = async (action, path) => {
    setBusy(action);
    try {
      const res = await api.post(`/verification-jobs/${job.id}/${path}`);
      const next = res.job || res;
      setJob(next);
      statusRef.current = next.status;
      if (action === 'retry') notify({ type: 'success', message: `Retrying ${res.retried} lead(s)` });
      if (action === 'cancel') {
        notify({ type: 'success', message: `Verification cancelled; ${res.released} lead(s) will send unverified` });
        onProgressRef.current?.(next);
      }
    } catch (e) {
      notify({ type: 'error', message: e.message });
    } finally {
      setBusy(null);
    }
  };

  const cancel = async () => {
    const ok = await confirm(
      'Cancel this verification? Leads that have not been checked yet (including failed ones) will be sent without verification.'
    );
    if (ok) act('cancel', 'cancel');
  };

  const dismiss = () => {
    setDismissedId(job.id);
    try { localStorage.setItem(dismissKey(campaignId), String(job.id)); } catch {}
  };

  if (!job) return null;
  const finished = job.status === 'completed' || job.status === 'cancelled';
  if (finished && dismissedId === job.id) return null;

  const badge = STATUS_BADGE[job.status] || STATUS_BADGE.queued;
  const processed = job.done + job.failed + job.cancelled;
  const pct = job.total ? Math.round((processed / job.total) * 100) : 0;
  const results = Object.entries(job.results || {}).sort((a, b) => b[1] - a[1]);

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 space-y-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 min-w-0">
          <h4 className="text-sm font-semibold text-gray-800 dark:text-gray-100">
            {job.reverify ? 'Re-verifying emails' : 'Verifying emails'}
          </h4>
          <span className={`text-[10px] border rounded-full px-2 py-0.5 font-medium ${badge.cls}`}>
            {badge.label}
          </span>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {ACTIVE.includes(job.status) && (
            <Button size="sm" variant="outline" onClick={() => act('pause', 'pause')} disabled={!!busy}>
              {busy === 'pause' ? 'Pausing…' : 'Pause'}
            </Button>
          )}
          {job.status === 'paused' && (
            <Button size="sm" onClick={() => act('resume', 'resume')} disabled={!!busy}>
              {busy === 'resume' ? 'Resuming…' : 'Resume'}
            </Button>
          )}
          {job.failed > 0 && job.status !== 'cancelled' && (
            <Button size="sm" variant="outline" onClick={() => act('retry', 'retry-failed')} disabled={!!busy}>
              {busy === 'retry' ? 'Retrying…' : `Retry failed (${job.failed})`}
            </Button>
          )}
          {!finished && (
            <Button size="sm" variant="ghost" className="text-red-600 hover:bg-red-50" onClick={cancel} disabled={!!busy}>
              Cancel
            </Button>
          )}
          {finished && (
            <button type="button" className="text-xs text-gray-400 hover:text-gray-600 px-1" onClick={dismiss}>
              Dismiss
            </button>
          )}
        </div>
      </div>

      <div>
        <div className="h-2 rounded-full bg-gray-100 dark:bg-gray-700 overflow-hidden flex">
          <div className="bg-teal-500 transition-all duration-500" style={{ width: `${job.total ? (job.done / job.total) * 100 : 0}%` }} />
          <div className="bg-red-400 transition-all duration-500" style={{ width: `${job.total ? (job.failed / job.total) * 100 : 0}%` }} />
          <div className="bg-gray-300 transition-all duration-500" style={{ width: `${job.total ? (job.cancelled / job.total) * 100 : 0}%` }} />
        </div>
        <div className="flex items-center gap-4 mt-2 text-xs text-gray-600 dark:text-gray-300 flex-wrap">
          <span><strong>{processed}</strong> / {job.total} processed ({pct}%)</span>
          <span><strong>{job.remaining}</strong> remaining</span>
          <span className="text-teal-700 dark:text-teal-400"><strong>{job.done}</strong> checked</span>
          {job.failed > 0 && (
            <button
              type="button"
              className="text-red-600 hover:underline"
              onClick={() => setShowFailed(v => !v)}
            >
              <strong>{job.failed}</strong> failed {showFailed ? '▾' : '▸'}
            </button>
          )}
          {job.cancelled > 0 && <span className="text-gray-400"><strong>{job.cancelled}</strong> cancelled</span>}
        </div>
      </div>

      {results.length > 0 && (
        <div className="flex items-center gap-3 flex-wrap text-xs">
          <span className="text-gray-400">Results:</span>
          {results.map(([status, n]) => (
            <span key={status} className={RESULT_STYLE[status] || 'text-gray-600'}>
              {status.replace('_', '-')} <strong>{n}</strong>
            </span>
          ))}
        </div>
      )}

      {job.status === 'paused' && job.last_error && (
        <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1.5">
          Paused: {job.last_error}. Fix it in Settings, then click Resume.
        </p>
      )}
      {job.status === 'paused' && !job.last_error && (
        <p className="text-xs text-gray-500">
          {job.reverify
            ? 'Paused. Leads keep their previous result until the job is resumed.'
            : "Paused. Leads waiting for verification won't be sent until the job is resumed or cancelled."}
        </p>
      )}
      {job.failed > 0 && job.status !== 'cancelled' && (
        <p className="text-xs text-gray-500">
          {job.reverify
            ? "Failed leads couldn't be re-checked (the request to the verification service failed) and keep their previous result until retried."
            : "Failed leads couldn't be checked (the request to the verification service failed) and won't be sent until they're retried or the job is cancelled."}
        </p>
      )}

      {showFailed && job.failed > 0 && (
        <div className="max-h-48 overflow-y-auto rounded border border-gray-200 dark:border-gray-700">
          <table className="w-full text-xs">
            <thead className="bg-gray-50 dark:bg-gray-900 sticky top-0">
              <tr>
                <th className="px-3 py-1.5 text-left font-medium text-gray-500">Email</th>
                <th className="px-3 py-1.5 text-left font-medium text-gray-500">Error</th>
                <th className="px-3 py-1.5 text-left font-medium text-gray-500">Attempts</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
              {failedItems.map(item => (
                <tr key={item.id}>
                  <td className="px-3 py-1.5 font-mono">{item.email}</td>
                  <td className="px-3 py-1.5 text-red-600">{item.message}</td>
                  <td className="px-3 py-1.5 text-gray-500">{item.attempts}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {job.failed > failedItems.length && (
            <p className="text-xs text-gray-400 px-3 py-1.5">Showing first {failedItems.length} of {job.failed}.</p>
          )}
        </div>
      )}
    </div>
  );
}
