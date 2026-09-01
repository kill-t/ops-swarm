<script setup lang="ts">
import { computed } from "vue";

import type { EvidenceBoardSnapshot, EvidenceItem } from "../types/evidence-board";

type ProvenanceStatus =
  | "matched"
  | "commit_not_observed"
  | "declared"
  | "not_declared"
  | "invalid";
type ArtifactStatus = "declared" | "not_declared" | "invalid";
type RuntimeArtifactStatus =
  | "matched"
  | "mismatched"
  | "unavailable"
  | "historical"
  | "not_declared"
  | "invalid";

interface DeploymentProvenanceItem {
  evidenceId: string;
  fromVersion: string | null;
  toVersion: string | null;
  repository: string | null;
  commitSha: string | null;
  imageRepository: string | null;
  imageDigest: string | null;
  artifactStatus: ArtifactStatus;
  runtimeImageRepository: string | null;
  runtimeImageDigest: string | null;
  runtimeInstanceId: string | null;
  runtimeStartedAt: string | null;
  runtimeArtifactStatus: RuntimeArtifactStatus;
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
const imageRepositoryPattern = /^[a-z0-9][a-z0-9._-]*(?::(?:[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?(?:\/[a-z0-9][a-z0-9._-]*)+$/;
const imageDigestPattern = /^sha256:[0-9a-f]{64}$/;
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
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
const artifactStatusLabels: Record<ArtifactStatus, string> = {
  declared: "已声明不可变摘要",
  not_declared: "历史记录未声明",
  invalid: "镜像来源异常",
};
const runtimeArtifactStatusLabels: Record<RuntimeArtifactStatus, string> = {
  matched: "当前运行身份一致",
  mismatched: "当前运行身份不一致",
  unavailable: "未取得运行身份",
  historical: "历史部署不参与当前核对",
  not_declared: "无构建产物可核对",
  invalid: "运行核对数据异常",
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

function artifactStatus(
  item: EvidenceItem,
  imageRepository: string | null,
  imageDigest: string | null,
): ArtifactStatus {
  const rawStatus = stringAttribute(item, "artifact_reference_status");
  if (rawStatus === "not_declared" || rawStatus === null) {
    return imageRepository === null && imageDigest === null ? "not_declared" : "invalid";
  }
  if (
    rawStatus === "declared" &&
    imageRepository !== null &&
    imageRepositoryPattern.test(imageRepository) &&
    imageDigest !== null &&
    imageDigestPattern.test(imageDigest)
  ) {
    return "declared";
  }
  return "invalid";
}

function runtimeArtifactStatus(
  item: EvidenceItem,
  declaredRepository: string | null,
  declaredDigest: string | null,
  artifactReferenceStatus: ArtifactStatus,
  runtimeRepository: string | null,
  runtimeDigest: string | null,
  runtimeInstanceId: string | null,
  runtimeStartedAt: string | null,
): RuntimeArtifactStatus {
  const rawStatus = stringAttribute(item, "runtime_artifact_status");
  const runtimeIdentitySource = stringAttribute(item, "runtime_identity_source");
  const runtimeFieldsAbsent =
    runtimeRepository === null &&
    runtimeDigest === null &&
    runtimeInstanceId === null &&
    runtimeStartedAt === null &&
    runtimeIdentitySource === null;
  if (artifactReferenceStatus === "not_declared") {
    return (rawStatus === null || rawStatus === "not_declared") && runtimeFieldsAbsent
      ? "not_declared"
      : "invalid";
  }
  if (artifactReferenceStatus !== "declared") return "invalid";
  if (rawStatus === "historical") {
    return runtimeFieldsAbsent ? "historical" : "invalid";
  }
  if (rawStatus === null || rawStatus === "unavailable") {
    return runtimeFieldsAbsent ? "unavailable" : "invalid";
  }
  if (
    (rawStatus !== "matched" && rawStatus !== "mismatched") ||
    runtimeRepository === null ||
    !imageRepositoryPattern.test(runtimeRepository) ||
    runtimeDigest === null ||
    !imageDigestPattern.test(runtimeDigest) ||
    runtimeInstanceId === null ||
    !uuidPattern.test(runtimeInstanceId) ||
    runtimeStartedAt === null ||
    !Number.isFinite(Date.parse(runtimeStartedAt)) ||
    runtimeIdentitySource !== "deployment-environment"
  ) {
    return "invalid";
  }
  const actuallyMatches =
    declaredRepository === runtimeRepository && declaredDigest === runtimeDigest;
  return (rawStatus === "matched") === actuallyMatches ? rawStatus : "invalid";
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
      const imageRepository = stringAttribute(item, "image_repository");
      const imageDigest = stringAttribute(item, "image_digest");
      const artifactReferenceStatus = artifactStatus(item, imageRepository, imageDigest);
      const runtimeImageRepository = stringAttribute(item, "runtime_image_repository");
      const runtimeImageDigest = stringAttribute(item, "runtime_image_digest");
      const runtimeInstanceId = stringAttribute(item, "runtime_instance_id");
      const runtimeStartedAt = stringAttribute(item, "runtime_started_at");
      const provenance = provenanceStatus(item, repository, commitSha);
      return {
        evidenceId: item.evidence_id,
        fromVersion: stringAttribute(item, "from_version"),
        toVersion: stringAttribute(item, "to_version"),
        repository,
        commitSha,
        imageRepository,
        imageDigest,
        artifactStatus: artifactReferenceStatus,
        runtimeImageRepository,
        runtimeImageDigest,
        runtimeInstanceId,
        runtimeStartedAt,
        runtimeArtifactStatus: runtimeArtifactStatus(
          item,
          imageRepository,
          imageDigest,
          artifactReferenceStatus,
          runtimeImageRepository,
          runtimeImageDigest,
          runtimeInstanceId,
          runtimeStartedAt,
        ),
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

function artifactStatusLabel(status: ArtifactStatus): string {
  return artifactStatusLabels[status];
}

function runtimeArtifactStatusLabel(status: RuntimeArtifactStatus): string {
  return runtimeArtifactStatusLabels[status];
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
          <div>
            <dt>容器镜像仓库</dt>
            <dd><code>{{ deployment.imageRepository ?? "—" }}</code></dd>
          </div>
          <div>
            <dt>不可变镜像摘要</dt>
            <dd><code>{{ deployment.imageDigest ?? "—" }}</code></dd>
          </div>
          <div>
            <dt>构建产物状态</dt>
            <dd>{{ artifactStatusLabel(deployment.artifactStatus) }}</dd>
          </div>
          <div>
            <dt>当前运行核对</dt>
            <dd
              class="runtime-artifact-status"
              :class="`runtime-artifact-status-${deployment.runtimeArtifactStatus}`"
            >
              {{ runtimeArtifactStatusLabel(deployment.runtimeArtifactStatus) }}
            </dd>
          </div>
          <div>
            <dt>运行镜像仓库</dt>
            <dd><code>{{ deployment.runtimeImageRepository ?? "—" }}</code></dd>
          </div>
          <div>
            <dt>运行镜像摘要</dt>
            <dd><code>{{ deployment.runtimeImageDigest ?? "—" }}</code></dd>
          </div>
          <div>
            <dt>运行实例 ID</dt>
            <dd><code>{{ deployment.runtimeInstanceId ?? "—" }}</code></dd>
          </div>
          <div>
            <dt>实例启动时间</dt>
            <dd>{{ deployment.runtimeStartedAt ? formatTime(deployment.runtimeStartedAt) : "—" }}</dd>
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
