const std = @import("std");
const sdk = @import("native_sdk");
const yogurt = @import("yogurt/theme.zig");
const revision_label = "work-timeline-v1";

const Scope = struct {
    executable: []const u8 = "",
    store: []const u8 = "",
    company: []const u8 = "",
};
const Timeline = struct {
    contract_revision: []const u8,
    company_id: []const u8,
    revision: []const u8,
    as_of_ns: i64,
    duties: []const struct {
        duty_id: []const u8,
        state: []const u8,
        owner_id: []const u8,
        due_at: ?i64,
        decision: ?struct { action: []const u8 },
    },
    paper: ?struct { booked_position: i64 },
    risk_control: ?RiskControl = null,
};
const RiskControl = struct {
    state: []const u8,
    effective_revision: ?u64,
};
const Duty = struct {
    duty_id: []const u8,
    state: []const u8,
    owner_id: []const u8,
    due: []const u8,
    decision: []const u8,
};
const Effects = sdk.Effects(Msg);
pub const Msg = union(enum) {
    activate,
    reply: sdk.EffectExit,
    deadline: sdk.EffectTimer,
    pub const view_unbound = .{ "reply", "deadline" };
};
pub const Model = struct {
    scope: Scope = .{},
    storage: std.heap.ArenaAllocator = .init(std.heap.page_allocator),
    request: u64 = 0,
    pending: bool = false,
    available: bool = false,
    status: []const u8 = "Company work unavailable.",
    observed: []const u8 = "",
    revision: []const u8 = "",
    investment: []const u8 = "",
    risk_control: []const u8 = "",
    show_risk_control: bool = false,
    duties: []const Duty = &.{},
    pub const view_unbound = .{ "scope", "storage", "request", "pending" };

    fn clear(self: *Model, status: []const u8) void {
        self.available = false;
        self.status = status;
        self.observed = "";
        self.revision = "";
        self.investment = "";
        self.risk_control = "";
        self.show_risk_control = false;
        self.duties = &.{};
        _ = self.storage.reset(.free_all);
    }

    fn present(self: *Model, body: []const u8) !void {
        const alloc = self.storage.allocator();
        const incoming = try std.json.parseFromSliceLeaky(Timeline, alloc, body, .{
            .ignore_unknown_fields = true,
            .allocate = .alloc_always,
        });
        if (!std.mem.eql(u8, incoming.contract_revision, revision_label) or
            !std.mem.eql(u8, incoming.company_id, self.scope.company))
            return error.IncompatibleTimeline;
        const rows = try alloc.alloc(Duty, incoming.duties.len);
        for (incoming.duties, rows) |item, *row| {
            row.* = .{
                .duty_id = item.duty_id,
                .state = item.state,
                .owner_id = item.owner_id,
                .due = if (item.due_at) |seconds| try clockText(alloc, seconds) else "Not scheduled",
                .decision = if (item.decision) |decision| decision.action else "Not recorded",
            };
        }
        if (incoming.as_of_ns <= 0 or incoming.revision.len != 64)
            return error.IncompleteTimeline;
        self.observed = try clockText(alloc, @divTrunc(incoming.as_of_ns, std.time.ns_per_s));
        self.revision = incoming.revision;
        self.investment = if (incoming.paper) |paper|
            try std.fmt.allocPrint(alloc, "Booked paper position: {d} instrument units", .{paper.booked_position})
        else
            "No investment observations in this view.";
        self.show_risk_control = incoming.risk_control != null;
        self.risk_control = if (incoming.risk_control) |control|
            try riskText(alloc, control)
        else
            "";
        self.duties = rows;
        self.available = true;
        self.status = if (rows.len == 0) "No work in this view." else "Work timeline · Read only";
    }
};

fn riskText(alloc: std.mem.Allocator, control: RiskControl) ![]const u8 {
    const revision = control.effective_revision orelse return "Paper risk status unavailable.";
    const label = if (std.mem.eql(u8, control.state, "suspended"))
        "New paper risk is suspended"
    else if (std.mem.eql(u8, control.state, "not_suspended"))
        "No active paper risk suspension"
    else
        return "Paper risk status unavailable.";
    return std.fmt.allocPrint(alloc, "{s} · Control revision {d}", .{ label, revision });
}

fn clockText(alloc: std.mem.Allocator, unix: i64) ![]const u8 {
    if (unix < 0 or unix > std.math.maxInt(i64) / std.time.ns_per_s)
        return error.InvalidObservationTime;
    const epoch = std.time.epoch.EpochSeconds{ .secs = @intCast(unix) };
    const year = epoch.getEpochDay().calculateYearDay();
    const day = year.calculateMonthDay();
    const clock = epoch.getDaySeconds();
    return std.fmt.allocPrint(alloc, "{d}-{d:0>2}-{d:0>2} {d:0>2}:{d:0>2}:{d:0>2} UTC", .{
        year.year,               day.month.numeric(),        @as(u6, day.day_index) + 1,
        clock.getHoursIntoDay(), clock.getMinutesIntoHour(), clock.getSecondsIntoMinute(),
    });
}

pub fn read(model: *Model, fx: *Effects) void {
    if (model.pending) return;
    const scope = model.scope;
    if (!std.fs.path.isAbsolute(scope.executable) or
        !std.fs.path.isAbsolute(scope.store) or scope.company.len == 0)
    {
        model.clear("No local company connection configured.");
        return;
    }
    model.clear("Checking current access…");
    model.request += 1;
    model.pending = true;
    const argv = [_][]const u8{ scope.executable, "inspect-work", scope.store, "--company-id", scope.company, "--contract-revision", revision_label };
    fx.spawn(.{ .key = model.request, .argv = &argv, .output = .collect, .on_exit = Effects.exitMsg(.reply) });
    fx.startTimer(.{ .key = model.request, .interval_ms = 5000, .on_fire = Effects.timerMsg(.deadline) });
}

pub fn update(model: *Model, msg: Msg, fx: *Effects) void {
    switch (msg) {
        .activate => read(model, fx),
        .reply => |result| {
            if (!model.pending or result.key != model.request) return;
            model.pending = false;
            fx.cancelTimer(result.key);
            if (result.reason != .exited or result.code != 0 or result.output_truncated) {
                model.clear("Company work unavailable. Refresh to try again.");
                return;
            }
            model.present(result.output) catch model.clear("Company work unavailable: invalid or incompatible response.");
        },
        .deadline => |timer| {
            if (!model.pending or timer.key != model.request) return;
            model.pending = false;
            fx.cancel(timer.key);
            model.clear("Company work unavailable: no response in time.");
        },
    }
}

pub fn tokens(_: *const Model) sdk.canvas.DesignTokens {
    return yogurt.theme(false, .neutral, false, false);
}

test "a compatible response reports missing observations without substituting zero" {
    var model = Model{ .scope = .{ .company = "company" } };
    defer model.storage.deinit();
    const body =
        \\{"contract_revision":"work-timeline-v1","company_id":"company","revision":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","as_of_ns":1700000000000000000,"duties":[{"duty_id":"d","owner_id":"owner","state":"due","due_at":null,"decision":null}],"paper":null}
    ;
    try model.present(body);
    try std.testing.expectEqualStrings("Not recorded", model.duties[0].decision);
    try std.testing.expectEqualStrings("2023-11-14 22:13:20 UTC", model.observed);
    try std.testing.expectEqualStrings("No investment observations in this view.", model.investment);
    try std.testing.expect(!model.show_risk_control);
    model.clear("Unavailable");
    model.scope.company = "other";
    try std.testing.expectError(error.IncompatibleTimeline, model.present(body));
    try std.testing.expect(!model.available and model.duties.len == 0);
}

test "risk status is shown only from a compatible service response" {
    var model = Model{ .scope = .{ .company = "company" } };
    defer model.storage.deinit();
    const body =
        \\{"contract_revision":"work-timeline-v1","company_id":"company","revision":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","as_of_ns":1700000000000000000,"duties":[],"paper":null,"risk_control":{"state":"suspended","effective_revision":3}}
    ;
    try model.present(body);
    try std.testing.expect(model.show_risk_control);
    try std.testing.expectEqualStrings("New paper risk is suspended · Control revision 3", model.risk_control);
    model.clear("Unavailable");
    try std.testing.expect(!model.show_risk_control);
    try std.testing.expectEqualStrings("", model.risk_control);
}

test "an old or incomplete reply cannot restore company work" {
    var model = Model{ .request = 2, .pending = true };
    defer model.storage.deinit();
    var fx: Effects = .{ .allocator = std.testing.allocator };
    update(&model, .{ .reply = .{ .key = 1, .code = 0, .output = "old response" } }, &fx);
    try std.testing.expect(model.pending and !model.available);
    update(&model, .{ .reply = .{ .key = 2, .code = 0, .output_truncated = true } }, &fx);
    try std.testing.expect(!model.pending and !model.available and model.duties.len == 0);
}
