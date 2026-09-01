import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import DeploymentProvenance from "./DeploymentProvenance.vue";
import type { EvidenceBoardSnapshot, EvidenceItem } from "../types/evidence-board";

const COMMIT_SHA = "a".repeat(40);
const IMAGE_DIGEST = `sha256:${"c".repeat(64)}`;
const RUNTIME_INSTANCE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

function evidenceItem(overrides: Partial<EvidenceItem>): EvidenceItem {
  return {
    id: "stored-evidence-001",
    evidence_id: "change-release-001",
    kind: "change",
    source: "change-simulator",
    summary: "order-service 已从 stable 发布为 rc1。",
    observed_at: "2026-08-31T08:00:00Z",
    attributes: {},
    content_hash: "b".repeat(64),
    created_at: "2026-08-31T08:00:01Z",
    ...overrides,
  };
}

function board(items: EvidenceItem[]): EvidenceBoardSnapshot {
  return {
    investigation_run_id: "run-001",
    status: "succeeded",
    evidence_items: items,
  };
}

describe("部署代码来源", () => {
  it("展示完整部署映射并只链接精确匹配的 GitHub Evidence", () => {
    const githubEvidenceId = `github-commit-${COMMIT_SHA}`;
    const deployment = evidenceItem({
      attributes: {
        change_type: "deployment",
        from_version: "stable",
        to_version: "2026.01.01-rc1",
        repository: "kill-t/ops-swarm",
        commit_sha: COMMIT_SHA,
        image_repository: "ghcr.io/kill-t/ops-swarm-order-service",
        image_digest: IMAGE_DIGEST,
        artifact_reference_status: "declared",
        runtime_artifact_status: "matched",
        runtime_image_repository: "ghcr.io/kill-t/ops-swarm-order-service",
        runtime_image_digest: IMAGE_DIGEST,
        runtime_instance_id: RUNTIME_INSTANCE_ID,
        runtime_started_at: "2026-08-31T07:59:00Z",
        runtime_identity_source: "deployment-environment",
        code_reference_status: "matched",
        github_commit_evidence_id: githubEvidenceId,
      },
    });
    const githubCommit = evidenceItem({
      id: "stored-evidence-002",
      evidence_id: githubEvidenceId,
      source: "github-rest",
      summary: "GitHub 提交证据。",
      attributes: {
        change_type: "source_commit",
        repository: "kill-t/ops-swarm",
        commit_sha: COMMIT_SHA,
        html_url: `https://github.com/kill-t/ops-swarm/commit/${COMMIT_SHA}`,
      },
    });

    const wrapper = mount(DeploymentProvenance, {
      props: { board: board([deployment, githubCommit]), loading: false, error: null },
    });

    expect(wrapper.text()).toContain("已精确匹配");
    expect(wrapper.text()).toContain("2026.01.01-rc1");
    expect(wrapper.text()).toContain("kill-t/ops-swarm");
    expect(wrapper.text()).toContain(COMMIT_SHA);
    expect(wrapper.text()).toContain("ghcr.io/kill-t/ops-swarm-order-service");
    expect(wrapper.text()).toContain(IMAGE_DIGEST);
    expect(wrapper.text()).toContain("已声明不可变摘要");
    expect(wrapper.text()).toContain("当前运行身份一致");
    expect(wrapper.text()).toContain(RUNTIME_INSTANCE_ID);
    const link = wrapper.get(".provenance-link");
    expect(link.attributes("href")).toBe(
      `https://github.com/kill-t/ops-swarm/commit/${COMMIT_SHA}`,
    );
    expect(link.attributes("rel")).toBe("noopener noreferrer");
  });

  it("明确区分未观察到提交和历史记录未声明来源", () => {
    const notObserved = evidenceItem({
      evidence_id: "change-release-not-observed",
      attributes: {
        change_type: "deployment",
        from_version: "stable",
        to_version: "rc2",
        repository: "kill-t/ops-swarm",
        commit_sha: COMMIT_SHA,
        code_reference_status: "commit_not_observed",
      },
    });
    const legacy = evidenceItem({
      id: "stored-evidence-legacy",
      evidence_id: "change-release-legacy",
      observed_at: "2026-08-30T08:00:00Z",
      attributes: {
        change_type: "deployment",
        from_version: "v1",
        to_version: "stable",
      },
    });

    const wrapper = mount(DeploymentProvenance, {
      props: { board: board([notObserved, legacy]), loading: false, error: null },
    });

    expect(wrapper.text()).toContain("时间窗未观察到");
    expect(wrapper.text()).toContain("不代表提交不存在");
    expect(wrapper.text()).toContain("未声明代码来源");
    expect(wrapper.text()).toContain("系统不会推测对应提交");
    expect(wrapper.text()).toContain("历史记录未声明");
    expect(wrapper.find(".provenance-link").exists()).toBe(false);
  });

  it("镜像仓库与摘要不完整时明确显示异常", () => {
    const deployment = evidenceItem({
      attributes: {
        change_type: "deployment",
        image_repository: "ghcr.io/kill-t/ops-swarm-order-service",
        artifact_reference_status: "declared",
      },
    });

    const wrapper = mount(DeploymentProvenance, {
      props: { board: board([deployment]), loading: false, error: null },
    });

    expect(wrapper.text()).toContain("镜像来源异常");
    expect(wrapper.text()).not.toContain("已声明不可变摘要");
  });

  it("镜像仓库端口越界时拒绝显示为可信声明", () => {
    const deployment = evidenceItem({
      attributes: {
        change_type: "deployment",
        image_repository: "registry.example.com:65536/team/order-service",
        image_digest: IMAGE_DIGEST,
        artifact_reference_status: "declared",
      },
    });

    const wrapper = mount(DeploymentProvenance, {
      props: { board: board([deployment]), loading: false, error: null },
    });

    expect(wrapper.text()).toContain("镜像来源异常");
  });

  it("运行镜像与部署声明不一致时明确告警", () => {
    const runtimeDigest = `sha256:${"d".repeat(64)}`;
    const deployment = evidenceItem({
      attributes: {
        change_type: "deployment",
        image_repository: "ghcr.io/kill-t/ops-swarm-order-service",
        image_digest: IMAGE_DIGEST,
        artifact_reference_status: "declared",
        runtime_artifact_status: "mismatched",
        runtime_image_repository: "ghcr.io/kill-t/ops-swarm-order-service",
        runtime_image_digest: runtimeDigest,
        runtime_instance_id: RUNTIME_INSTANCE_ID,
        runtime_started_at: "2026-08-31T07:59:00Z",
        runtime_identity_source: "deployment-environment",
      },
    });

    const wrapper = mount(DeploymentProvenance, {
      props: { board: board([deployment]), loading: false, error: null },
    });

    expect(wrapper.text()).toContain("当前运行身份不一致");
    expect(wrapper.text()).toContain(runtimeDigest);
    expect(wrapper.get(".runtime-artifact-status").classes()).toContain(
      "runtime-artifact-status-mismatched",
    );
  });

  it("后端匹配结论与字段矛盾时拒绝显示一致", () => {
    const deployment = evidenceItem({
      attributes: {
        change_type: "deployment",
        image_repository: "ghcr.io/kill-t/ops-swarm-order-service",
        image_digest: IMAGE_DIGEST,
        artifact_reference_status: "declared",
        runtime_artifact_status: "matched",
        runtime_image_repository: "ghcr.io/kill-t/ops-swarm-order-service",
        runtime_image_digest: `sha256:${"e".repeat(64)}`,
        runtime_instance_id: RUNTIME_INSTANCE_ID,
        runtime_started_at: "2026-08-31T07:59:00Z",
        runtime_identity_source: "deployment-environment",
      },
    });

    const wrapper = mount(DeploymentProvenance, {
      props: { board: board([deployment]), loading: false, error: null },
    });

    expect(wrapper.text()).toContain("运行核对数据异常");
    expect(wrapper.text()).not.toContain("当前运行身份一致");
  });

  it("历史部署保留摘要但不与当前实例比较", () => {
    const deployment = evidenceItem({
      attributes: {
        change_type: "deployment",
        image_repository: "ghcr.io/kill-t/ops-swarm-order-service",
        image_digest: IMAGE_DIGEST,
        artifact_reference_status: "declared",
        runtime_artifact_status: "historical",
      },
    });

    const wrapper = mount(DeploymentProvenance, {
      props: { board: board([deployment]), loading: false, error: null },
    });

    expect(wrapper.text()).toContain("历史部署不参与当前核对");
    expect(wrapper.text()).toContain(IMAGE_DIGEST);
  });

  it("关联引用不一致时拒绝宣称精确匹配", () => {
    const deployment = evidenceItem({
      attributes: {
        change_type: "deployment",
        repository: "kill-t/ops-swarm",
        commit_sha: COMMIT_SHA,
        code_reference_status: "matched",
        github_commit_evidence_id: "github-commit-mismatch",
      },
    });
    const mismatchedCommit = evidenceItem({
      evidence_id: "github-commit-mismatch",
      source: "github-rest",
      attributes: {
        change_type: "source_commit",
        repository: "kill-t/another-repository",
        commit_sha: COMMIT_SHA,
      },
    });

    const wrapper = mount(DeploymentProvenance, {
      props: { board: board([deployment, mismatchedCommit]), loading: false, error: null },
    });

    expect(wrapper.text()).toContain("关联数据异常");
    expect(wrapper.text()).not.toContain("已精确匹配");
    expect(wrapper.find(".provenance-link").exists()).toBe(false);
  });

  it("即使提交匹配也不渲染非白名单外链", () => {
    const githubEvidenceId = "github-commit-unsafe-url";
    const deployment = evidenceItem({
      attributes: {
        change_type: "deployment",
        repository: "kill-t/ops-swarm",
        commit_sha: COMMIT_SHA,
        code_reference_status: "matched",
        github_commit_evidence_id: githubEvidenceId,
      },
    });
    const githubCommit = evidenceItem({
      evidence_id: githubEvidenceId,
      source: "github-rest",
      attributes: {
        change_type: "source_commit",
        repository: "kill-t/ops-swarm",
        commit_sha: COMMIT_SHA,
        html_url: `https://example.com/kill-t/ops-swarm/commit/${COMMIT_SHA}`,
      },
    });

    const wrapper = mount(DeploymentProvenance, {
      props: { board: board([deployment, githubCommit]), loading: false, error: null },
    });

    expect(wrapper.text()).toContain("已精确匹配");
    expect(wrapper.find(".provenance-link").exists()).toBe(false);
  });
});
