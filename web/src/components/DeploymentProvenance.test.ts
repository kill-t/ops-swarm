import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import DeploymentProvenance from "./DeploymentProvenance.vue";
import type { EvidenceBoardSnapshot, EvidenceItem } from "../types/evidence-board";

const COMMIT_SHA = "a".repeat(40);

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
    expect(wrapper.find(".provenance-link").exists()).toBe(false);
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
