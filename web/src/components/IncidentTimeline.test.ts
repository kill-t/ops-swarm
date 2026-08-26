import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import IncidentTimeline from "./IncidentTimeline.vue";

describe("Incident 时间线", () => {
  it("展示连接状态和空时间线提示", () => {
    const wrapper = mount(IncidentTimeline, {
      props: {
        events: [],
        streamStatus: "reconnecting",
        streamError: null,
      },
    });

    expect(wrapper.text()).toContain("连接中断，自动重连中");
    expect(wrapper.text()).toContain("等待已持久化的事故事件");
  });
});
