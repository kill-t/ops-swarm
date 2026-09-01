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
  | "rolling_update"
  | "partial_mismatch"
  | "mismatched"
  | "unavailable"
  | "historical"
  | "not_declared"
  | "invalid";
type RuntimeInstanceClassification = "target" | "previous" | "unexpected";
type RuntimeProofStatus = "verified" | "unsigned" | "legacy_unsigned" | "invalid";

interface RuntimeInventoryInstance {
  instanceId: string;
  imageRepository: string;
  imageDigest: string;
  startedAt: string;
  classification: RuntimeInstanceClassification;
}

interface ParsedRuntimeInventory {
  present: boolean;
  valid: boolean;
  instances: RuntimeInventoryInstance[];
}

interface ParsedRuntimeProof {
  valid: boolean;
  status: RuntimeProofStatus;
  proofId: string | null;
  keyId: string | null;
  algorithm: string | null;
  issuedAt: string | null;
  expiresAt: string | null;
}

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
  runtimeInstances: RuntimeInventoryInstance[];
  runtimeInstanceCount: number | null;
  runtimeMatchingInstanceCount: number | null;
  runtimePreviousInstanceCount: number | null;
  runtimeUnexpectedInstanceCount: number | null;
  runtimeRolloutPhase: string | null;
  runtimeIdentitySource: string | null;
  runtimeProof: ParsedRuntimeProof | null;
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
const proofKeyIdPattern = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const maxRolloutDurationMilliseconds = 30 * 60 * 1000;
const maxProofDurationMilliseconds = 5 * 60 * 1000;
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
  matched: "全部运行实例一致",
  rolling_update: "受控滚动发布中",
  partial_mismatch: "部分运行实例不一致",
  mismatched: "全部运行实例不一致",
  unavailable: "未取得运行清单",
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

function integerAttribute(item: EvidenceItem, key: string): number | null {
  const value = item.attributes[key];
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value
    : null;
}

function hasAttribute(item: EvidenceItem, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(item.attributes, key);
}

function parseRuntimeInventory(item: EvidenceItem): ParsedRuntimeInventory {
  if (!hasAttribute(item, "runtime_instances")) {
    return { present: false, valid: true, instances: [] };
  }
  const rawInstances = item.attributes.runtime_instances;
  if (!Array.isArray(rawInstances) || rawInstances.length === 0 || rawInstances.length > 64) {
    return { present: true, valid: false, instances: [] };
  }
  const instances: RuntimeInventoryInstance[] = [];
  const instanceIds = new Set<string>();
  const expectedKeys = [
    "classification",
    "image_digest",
    "image_repository",
    "instance_id",
    "started_at",
  ];
  for (const rawInstance of rawInstances) {
    if (rawInstance === null || Array.isArray(rawInstance) || typeof rawInstance !== "object") {
      return { present: true, valid: false, instances: [] };
    }
    if (Object.keys(rawInstance).sort().join("|") !== expectedKeys.join("|")) {
      return { present: true, valid: false, instances: [] };
    }
    const instanceId = rawInstance.instance_id;
    const imageRepository = rawInstance.image_repository;
    const imageDigest = rawInstance.image_digest;
    const startedAt = rawInstance.started_at;
    const classification = rawInstance.classification;
    if (
      typeof instanceId !== "string" ||
      !uuidPattern.test(instanceId) ||
      instanceIds.has(instanceId) ||
      typeof imageRepository !== "string" ||
      !imageRepositoryPattern.test(imageRepository) ||
      typeof imageDigest !== "string" ||
      !imageDigestPattern.test(imageDigest) ||
      typeof startedAt !== "string" ||
      !Number.isFinite(Date.parse(startedAt)) ||
      (classification !== "target" &&
        classification !== "previous" &&
        classification !== "unexpected")
    ) {
      return { present: true, valid: false, instances: [] };
    }
    instanceIds.add(instanceId);
    instances.push({ instanceId, imageRepository, imageDigest, startedAt, classification });
  }
  return { present: true, valid: true, instances };
}

const proofDetailFieldNames = [
  "runtime_proof_id",
  "runtime_proof_key_id",
  "runtime_proof_algorithm",
  "runtime_proof_issued_at",
  "runtime_proof_expires_at",
];

function invalidRuntimeProof(): ParsedRuntimeProof {
  return {
    valid: false,
    status: "invalid",
    proofId: null,
    keyId: null,
    algorithm: null,
    issuedAt: null,
    expiresAt: null,
  };
}

function parseRuntimeProof(
  item: EvidenceItem,
  identitySource: string | null,
  reportedTimestamp: number,
): ParsedRuntimeProof {
  const rawStatus = stringAttribute(item, "runtime_proof_status");
  const proofId = stringAttribute(item, "runtime_proof_id");
  const keyId = stringAttribute(item, "runtime_proof_key_id");
  const algorithm = stringAttribute(item, "runtime_proof_algorithm");
  const issuedAt = stringAttribute(item, "runtime_proof_issued_at");
  const expiresAt = stringAttribute(item, "runtime_proof_expires_at");
  const statusPresent = hasAttribute(item, "runtime_proof_status");
  const detailsAbsent = proofDetailFieldNames.every((key) => !hasAttribute(item, key));
  if (identitySource === "deployment-environment") {
    if (
      !detailsAbsent ||
      (statusPresent && rawStatus !== "unsigned") ||
      (!statusPresent && rawStatus !== null)
    ) {
      return invalidRuntimeProof();
    }
    return {
      valid: true,
      status: rawStatus === "unsigned" ? "unsigned" : "legacy_unsigned",
      proofId: null,
      keyId: null,
      algorithm: null,
      issuedAt: null,
      expiresAt: null,
    };
  }
  if (
    identitySource !== "deployment-controller-ed25519" ||
    rawStatus !== "verified" ||
    proofId === null ||
    !uuidPattern.test(proofId) ||
    keyId === null ||
    !proofKeyIdPattern.test(keyId) ||
    algorithm !== "Ed25519" ||
    issuedAt === null ||
    expiresAt === null
  ) {
    return invalidRuntimeProof();
  }
  const issuedTimestamp = Date.parse(issuedAt);
  const expiresTimestamp = Date.parse(expiresAt);
  if (
    !Number.isFinite(issuedTimestamp) ||
    !Number.isFinite(expiresTimestamp) ||
    issuedTimestamp >= expiresTimestamp ||
    expiresTimestamp - issuedTimestamp > maxProofDurationMilliseconds ||
    issuedTimestamp > reportedTimestamp ||
    reportedTimestamp >= expiresTimestamp
  ) {
    return invalidRuntimeProof();
  }
  return {
    valid: true,
    status: "verified",
    proofId,
    keyId,
    algorithm,
    issuedAt,
    expiresAt,
  };
}

function legacyRuntimeArtifactStatus(
  rawStatus: string,
  declaredRepository: string | null,
  declaredDigest: string | null,
  runtimeRepository: string | null,
  runtimeDigest: string | null,
  runtimeInstanceId: string | null,
  runtimeStartedAt: string | null,
  runtimeIdentitySource: string | null,
): RuntimeArtifactStatus {
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

function runtimeArtifactStatus(
  item: EvidenceItem,
  declaredRepository: string | null,
  declaredDigest: string | null,
  artifactReferenceStatus: ArtifactStatus,
  runtimeRepository: string | null,
  runtimeDigest: string | null,
  runtimeInstanceId: string | null,
  runtimeStartedAt: string | null,
  inventory: ParsedRuntimeInventory,
): RuntimeArtifactStatus {
  const rawStatus = stringAttribute(item, "runtime_artifact_status");
  const runtimeIdentitySource = stringAttribute(item, "runtime_identity_source");
  const rolloutFieldNames = [
    "runtime_rollout_id",
    "runtime_rollout_started_at",
    "runtime_rollout_deadline_at",
    "runtime_rollout_target_image_repository",
    "runtime_rollout_target_image_digest",
  ];
  const inventoryFieldNames = [
    "runtime_instance_count",
    "runtime_matching_instance_count",
    "runtime_previous_instance_count",
    "runtime_unexpected_instance_count",
    "runtime_instances",
    "runtime_rollout_phase",
    "runtime_inventory_reported_at",
    "runtime_previous_image_repository",
    "runtime_previous_image_digest",
    ...rolloutFieldNames,
    "runtime_proof_status",
    ...proofDetailFieldNames,
  ];
  const inventoryFieldsAbsent = inventoryFieldNames.every((key) => !hasAttribute(item, key));
  const legacyFieldsAbsent =
    runtimeRepository === null &&
    runtimeDigest === null &&
    runtimeInstanceId === null &&
    runtimeStartedAt === null;
  const allRuntimeFieldsAbsent =
    legacyFieldsAbsent && inventoryFieldsAbsent && runtimeIdentitySource === null;
  if (artifactReferenceStatus === "not_declared") {
    return (rawStatus === null || rawStatus === "not_declared") && allRuntimeFieldsAbsent
      ? "not_declared"
      : "invalid";
  }
  if (artifactReferenceStatus !== "declared") return "invalid";
  if (rawStatus === "historical") {
    return allRuntimeFieldsAbsent ? "historical" : "invalid";
  }
  if (rawStatus === null || rawStatus === "unavailable") {
    return allRuntimeFieldsAbsent ? "unavailable" : "invalid";
  }
  if (!inventory.present) {
    return legacyRuntimeArtifactStatus(
      rawStatus,
      declaredRepository,
      declaredDigest,
      runtimeRepository,
      runtimeDigest,
      runtimeInstanceId,
      runtimeStartedAt,
      runtimeIdentitySource,
    );
  }
  if (
    !inventory.valid ||
    !legacyFieldsAbsent ||
    (runtimeIdentitySource !== "deployment-environment" &&
      runtimeIdentitySource !== "deployment-controller-ed25519") ||
    declaredRepository === null ||
    declaredDigest === null
  ) {
    return "invalid";
  }

  const instanceCount = integerAttribute(item, "runtime_instance_count");
  const matchingCount = integerAttribute(item, "runtime_matching_instance_count");
  const previousCount = integerAttribute(item, "runtime_previous_instance_count");
  const unexpectedCount = integerAttribute(item, "runtime_unexpected_instance_count");
  const previousRepository = stringAttribute(item, "runtime_previous_image_repository");
  const previousDigest = stringAttribute(item, "runtime_previous_image_digest");
  const hasValidPreviousArtifact =
    previousRepository !== null &&
    imageRepositoryPattern.test(previousRepository) &&
    previousDigest !== null &&
    imageDigestPattern.test(previousDigest);
  if ((previousRepository === null) !== (previousDigest === null)) return "invalid";

  let actualMatchingCount = 0;
  let actualPreviousCount = 0;
  let actualUnexpectedCount = 0;
  for (const instance of inventory.instances) {
    let classification: RuntimeInstanceClassification;
    if (
      instance.imageRepository === declaredRepository &&
      instance.imageDigest === declaredDigest
    ) {
      classification = "target";
      actualMatchingCount += 1;
    } else if (
      hasValidPreviousArtifact &&
      instance.imageRepository === previousRepository &&
      instance.imageDigest === previousDigest
    ) {
      classification = "previous";
      actualPreviousCount += 1;
    } else {
      classification = "unexpected";
      actualUnexpectedCount += 1;
    }
    if (instance.classification !== classification) return "invalid";
  }
  if (
    instanceCount !== inventory.instances.length ||
    matchingCount !== actualMatchingCount ||
    previousCount !== actualPreviousCount ||
    unexpectedCount !== actualUnexpectedCount
  ) {
    return "invalid";
  }

  const rolloutPhase = stringAttribute(item, "runtime_rollout_phase");
  const reportedAt = stringAttribute(item, "runtime_inventory_reported_at");
  const reportedTimestamp = reportedAt === null ? Number.NaN : Date.parse(reportedAt);
  if (
    (rolloutPhase !== "stable" && rolloutPhase !== "progressing") ||
    !Number.isFinite(reportedTimestamp) ||
    inventory.instances.some((instance) => Date.parse(instance.startedAt) > reportedTimestamp)
  ) {
    return "invalid";
  }
  const runtimeProof = parseRuntimeProof(item, runtimeIdentitySource, reportedTimestamp);
  if (!runtimeProof.valid) return "invalid";
  let rolloutActive = false;
  if (rolloutPhase === "stable") {
    if (rolloutFieldNames.some((key) => hasAttribute(item, key))) return "invalid";
  } else {
    const rolloutId = stringAttribute(item, "runtime_rollout_id");
    const rolloutStartedAt = stringAttribute(item, "runtime_rollout_started_at");
    const rolloutDeadlineAt = stringAttribute(item, "runtime_rollout_deadline_at");
    const rolloutTargetRepository = stringAttribute(
      item,
      "runtime_rollout_target_image_repository",
    );
    const rolloutTargetDigest = stringAttribute(item, "runtime_rollout_target_image_digest");
    const startedTimestamp = rolloutStartedAt === null ? Number.NaN : Date.parse(rolloutStartedAt);
    const deadlineTimestamp =
      rolloutDeadlineAt === null ? Number.NaN : Date.parse(rolloutDeadlineAt);
    if (
      rolloutId === null ||
      !uuidPattern.test(rolloutId) ||
      !Number.isFinite(startedTimestamp) ||
      !Number.isFinite(deadlineTimestamp) ||
      startedTimestamp >= deadlineTimestamp ||
      deadlineTimestamp - startedTimestamp > maxRolloutDurationMilliseconds ||
      startedTimestamp > reportedTimestamp ||
      rolloutTargetRepository === null ||
      !imageRepositoryPattern.test(rolloutTargetRepository) ||
      rolloutTargetDigest === null ||
      !imageDigestPattern.test(rolloutTargetDigest)
    ) {
      return "invalid";
    }
    rolloutActive =
      rolloutTargetRepository === declaredRepository &&
      rolloutTargetDigest === declaredDigest &&
      startedTimestamp <= reportedTimestamp &&
      reportedTimestamp <= deadlineTimestamp;
  }

  let expectedStatus: RuntimeArtifactStatus;
  if (actualMatchingCount === inventory.instances.length) {
    expectedStatus = "matched";
  } else if (
    actualMatchingCount > 0 &&
    actualPreviousCount > 0 &&
    actualUnexpectedCount === 0 &&
    rolloutActive
  ) {
    expectedStatus = "rolling_update";
  } else if (actualMatchingCount > 0) {
    expectedStatus = "partial_mismatch";
  } else {
    expectedStatus = "mismatched";
  }
  return rawStatus === expectedStatus ? expectedStatus : "invalid";
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
      const runtimeIdentitySource = stringAttribute(item, "runtime_identity_source");
      const runtimeInventory = parseRuntimeInventory(item);
      const validatedRuntimeStatus = runtimeArtifactStatus(
        item,
        imageRepository,
        imageDigest,
        artifactReferenceStatus,
        runtimeImageRepository,
        runtimeImageDigest,
        runtimeInstanceId,
        runtimeStartedAt,
        runtimeInventory,
      );
      const inventoryCanBeDisplayed =
        runtimeInventory.present &&
        runtimeInventory.valid &&
        validatedRuntimeStatus !== "invalid";
      const runtimeReportedAt = stringAttribute(item, "runtime_inventory_reported_at");
      const runtimeProof = runtimeInventory.present
        ? parseRuntimeProof(
            item,
            runtimeIdentitySource,
            runtimeReportedAt === null ? Number.NaN : Date.parse(runtimeReportedAt),
          )
        : null;
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
        runtimeInstances: inventoryCanBeDisplayed ? runtimeInventory.instances : [],
        runtimeInstanceCount: inventoryCanBeDisplayed
          ? integerAttribute(item, "runtime_instance_count")
          : null,
        runtimeMatchingInstanceCount: inventoryCanBeDisplayed
          ? integerAttribute(item, "runtime_matching_instance_count")
          : null,
        runtimePreviousInstanceCount: inventoryCanBeDisplayed
          ? integerAttribute(item, "runtime_previous_instance_count")
          : null,
        runtimeUnexpectedInstanceCount: inventoryCanBeDisplayed
          ? integerAttribute(item, "runtime_unexpected_instance_count")
          : null,
        runtimeRolloutPhase: inventoryCanBeDisplayed
          ? stringAttribute(item, "runtime_rollout_phase")
          : null,
        runtimeIdentitySource,
        runtimeProof:
          inventoryCanBeDisplayed && runtimeProof?.valid
            ? runtimeProof
            : runtimeInventory.present
              ? invalidRuntimeProof()
              : null,
        runtimeArtifactStatus: validatedRuntimeStatus,
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

function rolloutPhaseLabel(phase: string | null): string {
  return phase === "progressing" ? "滚动发布中" : phase === "stable" ? "稳定" : "—";
}

function instanceClassificationLabel(classification: RuntimeInstanceClassification): string {
  if (classification === "target") return "目标版本";
  if (classification === "previous") return "上一版本";
  return "非预期版本";
}

function runtimeProofStatusLabel(status: RuntimeProofStatus): string {
  if (status === "verified") return "Ed25519 已验签";
  if (status === "unsigned") return "未签名环境声明";
  if (status === "legacy_unsigned") return "历史未签名声明";
  return "签名证明异常";
}

function runtimeIdentitySourceLabel(source: string | null): string {
  if (source === "deployment-controller-ed25519") return "独立部署控制器";
  if (source === "deployment-environment") return "订单服务启动环境";
  return "—";
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
          <template v-if="deployment.runtimeInstances.length > 0">
            <div>
              <dt>运行实例总数</dt>
              <dd>{{ deployment.runtimeInstanceCount }}</dd>
            </div>
            <div>
              <dt>目标版本实例</dt>
              <dd>{{ deployment.runtimeMatchingInstanceCount }}</dd>
            </div>
            <div>
              <dt>上一版本实例</dt>
              <dd>{{ deployment.runtimePreviousInstanceCount }}</dd>
            </div>
            <div>
              <dt>非预期实例</dt>
              <dd>{{ deployment.runtimeUnexpectedInstanceCount }}</dd>
            </div>
            <div>
              <dt>发布阶段</dt>
              <dd>{{ rolloutPhaseLabel(deployment.runtimeRolloutPhase) }}</dd>
            </div>
            <div>
              <dt>运行清单证明</dt>
              <dd>{{ deployment.runtimeProof ? runtimeProofStatusLabel(deployment.runtimeProof.status) : "—" }}</dd>
            </div>
            <div>
              <dt>清单声明来源</dt>
              <dd>{{ runtimeIdentitySourceLabel(deployment.runtimeIdentitySource) }}</dd>
            </div>
            <div v-if="deployment.runtimeProof?.status === 'verified'">
              <dt>验签密钥编号</dt>
              <dd><code>{{ deployment.runtimeProof.keyId }}</code></dd>
            </div>
            <div v-if="deployment.runtimeProof?.status === 'verified'">
              <dt>证明有效期</dt>
              <dd>
                {{ deployment.runtimeProof.issuedAt ? formatTime(deployment.runtimeProof.issuedAt) : "—" }}
                →
                {{ deployment.runtimeProof.expiresAt ? formatTime(deployment.runtimeProof.expiresAt) : "—" }}
              </dd>
            </div>
          </template>
          <template v-else>
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
          </template>
          <template v-if="deployment.runtimeProof?.status === 'invalid'">
            <div>
              <dt>运行清单证明</dt>
              <dd>{{ runtimeProofStatusLabel(deployment.runtimeProof.status) }}</dd>
            </div>
            <div>
              <dt>清单声明来源</dt>
              <dd>{{ runtimeIdentitySourceLabel(deployment.runtimeIdentitySource) }}</dd>
            </div>
          </template>
        </dl>

        <ol v-if="deployment.runtimeInstances.length > 0" class="runtime-instance-list">
          <li
            v-for="instance in deployment.runtimeInstances"
            :key="instance.instanceId"
            :class="`runtime-instance-${instance.classification}`"
          >
            <span>{{ instanceClassificationLabel(instance.classification) }}</span>
            <code>{{ instance.instanceId }}</code>
            <code>{{ instance.imageRepository }}@{{ instance.imageDigest }}</code>
            <small>启动于 {{ formatTime(instance.startedAt) }}</small>
          </li>
        </ol>

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
