<script setup lang="ts">
import { computed } from "vue";

import type { EvidenceBoardSnapshot, EvidenceItem } from "../types/evidence-board";

type ProvenanceStatus =
  | "matched"
  | "commit_not_observed"
  | "declared"
  | "not_declared"
  | "invalid";

interface DeploymentProvenanceItem {
  evidenceId: string;
  fromVersion: string | null;
  toVersion: string | null;
  repository: string | null;
  commitSha: string | null;
  status: ProvenanceStatus;
  githubUrl: string | null;
  summary: string;
  observedAt: string;
}

const props = defineProps<{
  board: EvidenceBoardSnapshot | null;
  loading: boolean;
  error: string | null;
}>();

const repositoryPattern = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9_.-]{1,100}$/;
const commitShaPattern = /^[0-9a-f]{40,64}$/;
const statusLabels: Record<ProvenanceStatus, string> = {
  matched: "已精确匹配",
  commit_not_observed: "时间窗未观察到",
  declared: "等待 GitHub 关联",
  not_declared: "未声明代码来源",
  invalid: "关联数据异常",
};
const statusDescriptions: Record<ProvenanceStatus, string> = {
  matched: "部署声明已与同仓库、完整 SHA 的 GitHub Commit Evidence 对齐。",
  commit_not_observed: "当前调查时间窗没有观察到该提交，不代表提交不存在。",
  declared: "部署已声明代码来源，但当前数据源未执行 GitHub 关联。",
  not_declared: "历史部署未声明代码来源，系统不会推测对应提交。",
  invalid: "证据字段不完整或关联引用不一致，页面拒绝宣称已匹配。",
};

function stringAttribute(item: EvidenceItem, key: string): string | null {
  const value = item.attributes[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

function isValidRepository(value: string | null): value is string {
  if (value === null || !repositoryPattern.test(value)) return false;
  const repositoryName = value.split("/", 2)[1];
  return repositoryName !== "." && repositoryName !== "..";
}

function isValidCommitSha(value: string | null): value is string {
  return value !== null && commitShaPattern.test(value);
}

function safeGitHubCommitUrl(
  value: string | null,
  repository: string,
  commitSha: string,
): string | null {
  if (value === null) return null;
  try {
    const parsed = new URL(value);
    const expectedPath = `/${repository}/commit/${commitSha}`.toLowerCase();
    if (
      parsed.protocol !== "https:" ||
      parsed.hostname !== "github.com" ||
      parsed.port !== "" ||
      parsed.username !== "" ||
      parsed.password !== "" ||
      parsed.search !== "" ||
      parsed.hash !== "" ||
      parsed.pathname.toLowerCase() !== expectedPath
    ) {
      return null;
    }
    return value;
  } catch {
    return null;
  }
}

function matchingGitHubEvidence(
  deployment: EvidenceItem,
  repository: string,
  commitSha: string,
): EvidenceItem | null {
  const linkedEvidenceId = stringAttribute(deployment, "github_commit_evidence_id");
  if (linkedEvidenceId === null || props.board === null) return null;
  const matched = props.board.evidence_items.find(
    (item) => item.evidence_id === linkedEvidenceId,
  );
  if (
    matched === undefined ||
    matched.kind !== "change" ||
    matched.source !== "github-rest" ||
    stringAttribute(matched, "change_type") !== "source_commit" ||
    stringAttribute(matched, "repository")?.toLowerCase() !== repository.toLowerCase() ||
    stringAttribute(matched, "commit_sha") !== commitSha
  ) {
    return null;
  }
  return matched;
}

function provenanceStatus(
  item: EvidenceItem,
  repository: string | null,
  commitSha: string | null,
): { status: ProvenanceStatus; githubUrl: string | null } {
  const rawStatus = stringAttribute(item, "code_reference_status");
  if (rawStatus === "not_declared") {
    return repository === null && commitSha === null
      ? { status: "not_declared", githubUrl: null }
      : { status: "invalid", githubUrl: null };
  }
  if (rawStatus === null && repository === null && commitSha === null) {
    return { status: "not_declared", githubUrl: null };
  }
  if (!isValidRepository(repository) || !isValidCommitSha(commitSha)) {
    return { status: "invalid", githubUrl: null };
  }
  if (rawStatus === "matched") {
    const githubEvidence = matchingGitHubEvidence(item, repository, commitSha);
    if (githubEvidence === null) {
      return { status: "invalid", githubUrl: null };
    }
    return {
      status: "matched",
      githubUrl: safeGitHubCommitUrl(
        stringAttribute(githubEvidence, "html_url"),
        repository,
        commitSha,
      ),
    };
  }
  if (rawStatus === "commit_not_observed" || rawStatus === "declared") {
    return { status: rawStatus, githubUrl: null };
  }
  return { status: "invalid", githubUrl: null };
}

const deployments = computed<DeploymentProvenanceItem[]>(() => {
  if (props.board === null) return [];
  return props.board.evidence_items
    .filter(
      (item) =>
        item.kind === "change" &&
        item.source === "change-simulator" &&
        stringAttribute(item, "change_type") === "deployment",
    )
    .map((item) => {
      const repository = stringAttribute(item, "repository");
      const commitSha = stringAttribute(item, "commit_sha");
      const provenance = provenanceStatus(item, repository, commitSha);
      return {
        evidenceId: item.evidence_id,
        fromVersion: stringAttribute(item, "from_version"),
        toVersion: stringAttribute(item, "to_version"),
        repository,
        commitSha,
        status: provenance.status,
        githubUrl: provenance.githubUrl,
        summary: item.summary,
        observedAt: item.observed_at,
      };
    })
    .sort((left, right) => Date.parse(right.observedAt) - Date.parse(left.observedAt));
});

function statusLabel(status: ProvenanceStatus): string {
  return statusLabels[status];
}

function statusDescription(status: ProvenanceStatus): string {
  return statusDescriptions[status];
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}
</script>

<template>
  <section class="provenance-panel" aria-labelledby="deployment-provenance-title">
    <div class="provenance-heading">
      <div>
        <p class="eyebrow">SOFTWARE PROVENANCE</p>
        <h3 id="deployment-provenance-title">部署代码来源</h3>
      </div>
      <span>{{ deployments.length }} 条部署事实</span>
    </div>

    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-if="loading && board === null" class="empty-state" aria-live="polite">
      正在读取持久化 Evidence Board…
    </p>
    <p v-else-if="board === null" class="empty-state">尚未取得 Evidence Board。</p>
    <p
      v-else-if="deployments.length === 0 && (board.status === 'queued' || board.status === 'running')"
      class="empty-state"
    >
      调查正在收集部署证据。
    </p>
    <p v-else-if="deployments.length === 0" class="empty-state">
      本次调查没有部署变更证据。
    </p>

    <ol v-else class="provenance-list" :aria-busy="loading">
      <li v-for="deployment in deployments" :key="deployment.evidenceId" class="provenance-card">
        <header class="provenance-card-heading">
          <div>
            <span class="stage-index">DEPLOYMENT</span>
            <strong>{{ deployment.toVersion ?? "未知目标版本" }}</strong>
          </div>
          <span
            class="stage-status"
            :class="`provenance-status-${deployment.status}`"
          >
            {{ statusLabel(deployment.status) }}
          </span>
        </header>

        <p class="provenance-summary">{{ deployment.summary }}</p>
        <dl class="provenance-facts">
          <div>
            <dt>版本迁移</dt>
            <dd>{{ deployment.fromVersion ?? "—" }} → {{ deployment.toVersion ?? "—" }}</dd>
          </div>
          <div>
            <dt>代码仓库</dt>
            <dd><code>{{ deployment.repository ?? "—" }}</code></dd>
          </div>
          <div>
            <dt>完整 Commit SHA</dt>
            <dd><code>{{ deployment.commitSha ?? "—" }}</code></dd>
          </div>
        </dl>

        <a
          v-if="deployment.githubUrl"
          class="provenance-link"
          :href="deployment.githubUrl"
          target="_blank"
          rel="noopener noreferrer"
        >
          查看只读 GitHub 提交证据 ↗
        </a>
        <footer class="provenance-note">
          <span>{{ statusDescription(deployment.status) }}</span>
          <small>观察于 {{ formatTime(deployment.observedAt) }}</small>
        </footer>
      </li>
    </ol>
  </section>
</template>
