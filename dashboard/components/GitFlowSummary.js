// Shared Git Flow Summary component — used in GraphDetailPanel (compact) and TaskDetail
import { html } from './utils.js';

export function GitFlowSummary({ task, compact = false }) {
    const rc = task.resolved_config;
    if (!rc) return null;

    const defaultBranch = task.project_default_branch || 'main';
    const baseBranch = rc.base_branch || defaultBranch;
    const mergeTarget = task.branch_target || baseBranch;
    const autoMerge = rc.auto_merge;
    const autoPr = rc.auto_pr;
    const isIntegration = mergeTarget !== defaultBranch;

    let flowSuffix, whenDone;
    if (autoMerge) {
        flowSuffix = `\u2192 ${mergeTarget} (auto-merge)`;
        whenDone = isIntegration
            ? `code lands on ${mergeTarget} automatically. You'll need to merge ${mergeTarget} \u2192 ${defaultBranch} separately.`
            : `code lands on ${mergeTarget} automatically. Nothing for you to do.`;
    } else if (autoPr) {
        flowSuffix = `\u2192 ${mergeTarget} (auto-PR)`;
        whenDone = `a PR is opened to ${mergeTarget}. Review and merge it on GitHub.`;
    } else {
        flowSuffix = `\u2192 manual`;
        whenDone = `branch is pushed to origin. Open a PR or merge manually.`;
    }

    if (compact) {
        return html`
            <div class="text-xs text-slate-500 mt-1 mb-2 space-y-0.5">
                <div>\uD83D\uDD00 branched from <span class="font-mono text-slate-400">${baseBranch}</span> ${flowSuffix}</div>
                <div>When done: ${whenDone}</div>
            </div>
        `;
    }

    return html`
        <div class="text-sm text-slate-400 mt-2 space-y-0.5">
            <div>\uD83D\uDD00 branched from <span class="font-mono">${baseBranch}</span> ${flowSuffix}</div>
            <div class="text-slate-500">When done: ${whenDone}</div>
        </div>
    `;
}
