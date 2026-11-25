const std = @import("std");
const sdk = @import("native_sdk");
const yogurt = @import("yogurt/theme.zig");
pub const pending_store = @import("pending.zig");

const revision_label = "work-timeline-v1";

pub const Scope = struct {
    executable: []const u8 = "",
    store: []const u8 = "",
    socket: []const u8 = "",
    company: []const u8 = "",
    store_key: []const u8 = "",
    pending_dir: []const u8 = "",

    pub fn hasValidTransport(self: Scope) bool {
        if (!std.fs.path.isAbsolute(self.executable) or self.company.len == 0) {
            return false;
        }
        if (self.socket.len > 0) {
            return std.fs.path.isAbsolute(self.socket);
        }
        return std.fs.path.isAbsolute(self.store);
    }
};

pub const LinkState = enum {
    disconnected,
    connecting,
    connected,
    incompatible,
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

/// Narrow transport boundary routing through the UDS daemon client-bridge when a
/// daemon socket is configured, with local CLI fallback when no socket is set.
pub const Transport = struct {
    pub fn inspectWork(scope: Scope, key: u64, fx: *Effects) void {
        if (scope.socket.len > 0) {
            const argv = [_][]const u8{
                scope.executable,
                "client-bridge",
                "--socket",
                scope.socket,
                "inspect-work",
                "--company-id",
                scope.company,
                "--contract-revision",
                revision_label,
            };
            fx.spawn(.{ .key = key, .argv = &argv, .output = .collect, .on_exit = Effects.exitMsg(.reply) });
        } else {
            const argv = [_][]const u8{
                scope.executable,
                "inspect-work",
                scope.store,
                "--company-id",
                scope.company,
                "--contract-revision",
                revision_label,
            };
            fx.spawn(.{ .key = key, .argv = &argv, .output = .collect, .on_exit = Effects.exitMsg(.reply) });
        }
        fx.startTimer(.{ .key = key, .interval_ms = 5000, .on_fire = Effects.timerMsg(.deadline) });
    }

    pub fn readCommandReceipt(
        scope: Scope,
        command_id: []const u8,
        operation: []const u8,
        key: u64,
        fx: *Effects,
    ) void {
        if (scope.socket.len > 0) {
            const argv = [_][]const u8{
                scope.executable,
                "client-bridge",
                "--socket",
                scope.socket,
                "read-command-receipt",
                "--company-id",
                scope.company,
                "--command-id",
                command_id,
                "--operation",
                operation,
            };
            fx.spawn(.{ .key = key, .argv = &argv, .output = .collect, .on_exit = Effects.exitMsg(.receipt_reply) });
        } else {
            const argv = [_][]const u8{
                scope.executable,
                "read-command-receipt",
                scope.store,
                "--company-id",
                scope.company,
                "--command-id",
                command_id,
                "--operation",
                operation,
            };
            fx.spawn(.{ .key = key, .argv = &argv, .output = .collect, .on_exit = Effects.exitMsg(.receipt_reply) });
        }
        fx.startTimer(.{ .key = key, .interval_ms = 5000, .on_fire = Effects.timerMsg(.receipt_deadline) });
    }
};

const Effects = sdk.Effects(Msg);

pub const Msg = union(enum) {
    activate,
    reply: sdk.EffectExit,
    deadline: sdk.EffectTimer,
    receipt_reply: sdk.EffectExit,
    receipt_deadline: sdk.EffectTimer,
    pub const view_unbound = .{ "reply", "deadline", "receipt_reply", "receipt_deadline" };
};

pub const Model = struct {
    scope: Scope = .{},
    storage: std.heap.ArenaAllocator = .init(std.heap.page_allocator),
    command_storage: std.heap.ArenaAllocator = .init(std.heap.page_allocator),
    request_scope_storage: std.heap.ArenaAllocator = .init(std.heap.page_allocator),
    next_effect_key: u64 = 0,
    request: u64 = 0,
    receipt_request: u64 = 0,
    pending: bool = false,
    receipt_pending: bool = false,
    pending_recovered: bool = false,
    request_company_id: []const u8 = "",
    request_store_key: []const u8 = "",
    request_socket: []const u8 = "",

    // Separated Link State (never blank-wipes verified Data State on transient loss).
    link_state: LinkState = .disconnected,
    stale: bool = false,
    controls_locked: bool = true,
    show_stale_banner: bool = false,
    stale_banner: []const u8 = "",

    // Verified Data State stamped with (company_id, store_key, socket, revision, as_of_ns).
    available: bool = false,
    data_company_id: []const u8 = "",
    data_store_key: []const u8 = "",
    data_socket: []const u8 = "",
    data_as_of_ns: i64 = 0,
    status: []const u8 = "Company work unavailable.",
    observed: []const u8 = "",
    revision: []const u8 = "",
    investment: []const u8 = "",
    risk_control: []const u8 = "",
    show_risk_control: bool = false,
    duties: []const Duty = &.{},

    // Pending board command lifecycle (never auto-reissues command_id on restart/timeout).
    command_state: pending_store.CommandLifecycle = .idle,
    pending_command: ?pending_store.PendingCommand = null,
    show_command_status: bool = false,
    command_status: []const u8 = "",

    pub const view_unbound = .{
        "scope",
        "storage",
        "command_storage",
        "request_scope_storage",
        "next_effect_key",
        "request",
        "receipt_request",
        "pending",
        "receipt_pending",
        "pending_recovered",
        "request_company_id",
        "request_store_key",
        "request_socket",
        "link_state",
        "stale",
        "controls_locked",
        "data_company_id",
        "data_store_key",
        "data_socket",
        "data_as_of_ns",
        "command_state",
        "pending_command",
    };

    pub fn deinit(self: *Model) void {
        self.storage.deinit();
        self.command_storage.deinit();
        self.request_scope_storage.deinit();
    }

    fn nextKey(self: *Model) u64 {
        self.next_effect_key += 1;
        return self.next_effect_key;
    }

    fn effectiveStoreKey(scope: Scope) []const u8 {
        if (scope.store_key.len > 0) return scope.store_key;
        if (scope.socket.len > 0) return scope.socket;
        return scope.store;
    }

    fn scopeMatchesData(self: *const Model) bool {
        if (!self.available) return false;
        return std.mem.eql(u8, self.data_company_id, self.scope.company) and
            std.mem.eql(u8, self.data_store_key, effectiveStoreKey(self.scope)) and
            std.mem.eql(u8, self.data_socket, self.scope.socket);
    }

    fn scopeMatchesInFlight(self: *const Model) bool {
        if (self.request_company_id.len == 0) return true;
        return std.mem.eql(u8, self.request_company_id, self.scope.company) and
            std.mem.eql(u8, self.request_store_key, effectiveStoreKey(self.scope)) and
            std.mem.eql(u8, self.request_socket, self.scope.socket);
    }

    fn stampInFlightScope(self: *Model) void {
        _ = self.request_scope_storage.reset(.free_all);
        const alloc = self.request_scope_storage.allocator();
        self.request_company_id = alloc.dupe(u8, self.scope.company) catch "";
        self.request_store_key = alloc.dupe(u8, effectiveStoreKey(self.scope)) catch "";
        self.request_socket = alloc.dupe(u8, self.scope.socket) catch "";
    }

    fn clearInMemoryCommandState(self: *Model) void {
        // Clears in-memory command state without deleting any on-disk pending file.
        self.pending_command = null;
        self.command_state = .idle;
        self.receipt_pending = false;
        self.receipt_request = 0;
        self.pending_recovered = false;
        self.show_command_status = false;
        self.command_status = "";
        _ = self.command_storage.reset(.free_all);
    }

    fn computeControlsLocked(self: *const Model) bool {
        if (self.link_state != .connected or self.stale or !self.scopeMatchesData()) {
            return true;
        }
        return self.command_state != .idle and
            self.command_state != .applied and
            self.command_state != .denied;
    }

    pub fn clearData(self: *Model, status: []const u8, link: LinkState) void {
        self.available = false;
        self.link_state = link;
        self.stale = false;
        self.controls_locked = true;
        self.show_stale_banner = false;
        self.stale_banner = "";
        self.data_company_id = "";
        self.data_store_key = "";
        self.data_socket = "";
        self.data_as_of_ns = 0;
        self.status = status;
        self.observed = "";
        self.revision = "";
        self.investment = "";
        self.risk_control = "";
        self.show_risk_control = false;
        self.duties = &.{};
        _ = self.storage.reset(.free_all);
        self.clearInMemoryCommandState();
    }

    pub fn markTransientDisconnect(self: *Model, reason: []const u8) void {
        self.link_state = .disconnected;
        self.controls_locked = true;
        if (self.scopeMatchesData()) {
            self.stale = true;
            self.show_stale_banner = true;
            self.stale_banner = reason;
            return;
        }
        self.clearData(reason, .disconnected);
    }

    pub fn present(self: *Model, body: []const u8) !void {
        var next_arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
        errdefer next_arena.deinit();
        const alloc = next_arena.allocator();

        const incoming = try std.json.parseFromSliceLeaky(Timeline, alloc, body, .{
            .ignore_unknown_fields = true,
            .allocate = .alloc_always,
        });
        if (!std.mem.eql(u8, incoming.contract_revision, revision_label) or
            !std.mem.eql(u8, incoming.company_id, self.scope.company))
        {
            return error.IncompatibleTimeline;
        }
        if (incoming.as_of_ns <= 0 or incoming.revision.len != 64) {
            return error.IncompleteTimeline;
        }

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
        const observed_text = try clockText(alloc, @divTrunc(incoming.as_of_ns, std.time.ns_per_s));
        const investment_text = if (incoming.paper) |paper|
            try std.fmt.allocPrint(alloc, "Booked paper position: {d} instrument units", .{paper.booked_position})
        else
            "No investment observations in this view.";
        const show_risk = incoming.risk_control != null;
        const risk_text = if (incoming.risk_control) |control|
            try riskText(alloc, control)
        else
            "";
        const stamped_company = try alloc.dupe(u8, incoming.company_id);
        const stamped_store = try alloc.dupe(u8, effectiveStoreKey(self.scope));
        const stamped_socket = try alloc.dupe(u8, self.scope.socket);

        // Commit new Data State atomically without blanking prior state on parse error.
        self.storage.deinit();
        self.storage = next_arena;

        self.data_company_id = stamped_company;
        self.data_store_key = stamped_store;
        self.data_socket = stamped_socket;
        self.data_as_of_ns = incoming.as_of_ns;
        self.observed = observed_text;
        self.revision = incoming.revision;
        self.investment = investment_text;
        self.show_risk_control = show_risk;
        self.risk_control = risk_text;
        self.duties = rows;
        self.available = true;
        self.link_state = .connected;
        self.stale = false;
        self.show_stale_banner = false;
        self.stale_banner = "";
        self.controls_locked = self.computeControlsLocked();
        self.status = if (rows.len == 0) "No work in this view." else "Work timeline · Read only";
    }

    pub fn stagePendingCommand(
        self: *Model,
        io: std.Io,
        command_id: []const u8,
        command_type: []const u8,
        payload: []const u8,
        expected_revision: u64,
        sent_ns: i64,
    ) !void {
        if (self.controls_locked or self.link_state != .connected or !self.scopeMatchesData()) {
            return error.ControlsLocked;
        }
        if (self.pending_command != null) {
            return error.PendingCommandInFlight;
        }
        if (self.scope.pending_dir.len == 0) {
            return error.PendingDirectoryUnconfigured;
        }
        var next_arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
        errdefer next_arena.deinit();
        const alloc = next_arena.allocator();
        const digest = pending_store.payloadSha256Hex(payload);
        const cmd = pending_store.PendingCommand{
            .command_id = try alloc.dupe(u8, command_id),
            .command_type = try alloc.dupe(u8, command_type),
            .payload = try alloc.dupe(u8, payload),
            .payload_sha256 = try alloc.dupe(u8, &digest),
            .expected_revision = expected_revision,
            .sent_ns = sent_ns,
        };
        try pending_store.savePendingAtomic(io, alloc, self.scope.pending_dir, self.scope.company, cmd);
        self.command_storage.deinit();
        self.command_storage = next_arena;
        self.pending_command = cmd;
        self.pending_recovered = true;
        self.command_state = .submitting;
        self.controls_locked = true;
        self.show_command_status = true;
        self.command_status = "Submitting board command…";
    }

    pub fn markCommandUnknown(self: *Model, fx: *Effects) void {
        const cmd = self.pending_command orelse return;
        self.command_state = .unknown;
        self.controls_locked = true;
        self.show_command_status = true;
        self.command_status = "Command outcome unknown; checking saved receipt without reissuing.";
        if (self.scope.hasValidTransport()) {
            if (self.receipt_pending) {
                fx.cancel(self.receipt_request);
                fx.cancelTimer(self.receipt_request);
            }
            self.receipt_request = self.nextKey();
            self.receipt_pending = true;
            Transport.readCommandReceipt(self.scope, cmd.command_id, cmd.command_type, self.receipt_request, fx);
        }
    }

    pub fn recoverPendingCommand(self: *Model, io: std.Io, fx: *Effects) !void {
        if (self.scope.pending_dir.len == 0 or self.scope.company.len == 0) return;
        if (self.receipt_pending) {
            fx.cancel(self.receipt_request);
            fx.cancelTimer(self.receipt_request);
        }
        // Clear pending pointer BEFORE freeing backing arena to prevent use-after-free.
        self.clearInMemoryCommandState();
        self.pending_recovered = true;

        var next_arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
        errdefer next_arena.deinit();
        const alloc = next_arena.allocator();
        const loaded = pending_store.loadPending(io, alloc, self.scope.pending_dir, self.scope.company) catch |err| {
            self.command_state = .unknown;
            self.controls_locked = true;
            self.show_command_status = true;
            self.command_status = "Saved pending command is unreadable; controls remain locked.";
            return err;
        };
        if (loaded) |cmd| {
            self.command_storage.deinit();
            self.command_storage = next_arena;
            self.pending_command = cmd;
            self.markCommandUnknown(fx);
        } else {
            next_arena.deinit();
        }
    }

    pub fn applyReceiptOutput(self: *Model, io: std.Io, output: []const u8) void {
        const current = self.pending_command orelse return;
        var parse_arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
        defer parse_arena.deinit();
        const alloc = parse_arena.allocator();

        const parsed = pending_store.parseReceiptStatus(alloc, output) catch {
            self.command_state = .unknown;
            self.controls_locked = true;
            self.command_status = "Command receipt unreadable; controls remain locked.";
            return;
        };
        if (!std.mem.eql(u8, parsed.company_id, self.scope.company) or
            !std.mem.eql(u8, parsed.command_id, current.command_id))
        {
            self.command_state = .unknown;
            self.controls_locked = true;
            self.command_status = "Command receipt scope mismatch; controls remain locked.";
            return;
        }
        if (std.mem.eql(u8, parsed.status, "not_found")) {
            self.command_state = .not_found_retry_choice;
            self.controls_locked = true;
            self.show_command_status = true;
            self.command_status = "Command receipt not found. Retry same command ID or discard draft.";
            return;
        }
        if (!std.mem.eql(u8, parsed.status, "found")) {
            self.command_state = .unknown;
            self.controls_locked = true;
            self.command_status = "Unexpected command receipt status; controls remain locked.";
            return;
        }
        const cmd_type = parsed.command_type orelse {
            self.command_state = .unknown;
            self.controls_locked = true;
            self.command_status = "Command receipt missing operation type; controls remain locked.";
            return;
        };
        if (!std.mem.eql(u8, cmd_type, current.command_type)) {
            self.command_state = .unknown;
            self.controls_locked = true;
            self.command_status = "Command receipt operation mismatch; controls remain locked.";
            return;
        }
        const state = parsed.state orelse {
            self.command_state = .unknown;
            self.controls_locked = true;
            self.command_status = "Command receipt missing state; controls remain locked.";
            return;
        };
        if (std.mem.eql(u8, state, "applied") or std.mem.eql(u8, state, "denied")) {
            if (self.scope.pending_dir.len > 0) {
                pending_store.clearPending(io, alloc, self.scope.pending_dir, self.scope.company) catch {
                    self.command_state = .unknown;
                    self.controls_locked = true;
                    self.show_command_status = true;
                    self.command_status = "Unable to clear saved pending file; controls remain locked.";
                    return;
                };
            }
            self.pending_command = null;
            _ = self.command_storage.reset(.free_all);
            if (std.mem.eql(u8, state, "applied")) {
                self.command_state = .applied;
                self.command_status = "Board command applied.";
            } else {
                self.command_state = .denied;
                self.command_status = "Board command denied by company authority or revision check.";
            }
            self.controls_locked = self.computeControlsLocked();
            self.show_command_status = true;
        } else if (std.mem.eql(u8, state, "accepted")) {
            self.command_state = .accepted;
            self.controls_locked = true;
            self.show_command_status = true;
            self.command_status = "Board command accepted and pending settlement.";
        } else {
            self.command_state = .unknown;
            self.controls_locked = true;
            self.show_command_status = true;
            self.command_status = "Unknown command receipt state; controls remain locked.";
        }
    }

    pub fn retryPendingSameId(self: *Model) !pending_store.PendingCommand {
        if (self.command_state != .not_found_retry_choice) return error.RetryNotPermitted;
        const cmd = self.pending_command orelse return error.MissingPendingCommand;
        self.command_state = .submitting;
        self.controls_locked = true;
        self.show_command_status = true;
        self.command_status = "Retrying original command ID…";
        return cmd;
    }

    pub fn discardPendingDraft(self: *Model, io: std.Io) !void {
        if (self.command_state != .not_found_retry_choice) return error.DiscardNotPermitted;
        var tmp_arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
        defer tmp_arena.deinit();
        if (self.scope.pending_dir.len > 0) {
            try pending_store.clearPending(io, tmp_arena.allocator(), self.scope.pending_dir, self.scope.company);
        }
        self.pending_command = null;
        _ = self.command_storage.reset(.free_all);
        self.command_state = .idle;
        self.controls_locked = self.computeControlsLocked();
        self.show_command_status = false;
        self.command_status = "";
    }
};

fn isAccessDenialOutput(text: []const u8) bool {
    return std.mem.indexOf(u8, text, "HTTP 401") != null or
        std.mem.indexOf(u8, text, "HTTP 403") != null or
        std.mem.indexOf(u8, text, "401 Unauthorized") != null or
        std.mem.indexOf(u8, text, "403 Forbidden") != null or
        std.mem.indexOf(u8, text, "Unauthorized") != null or
        std.mem.indexOf(u8, text, "Forbidden") != null or
        std.mem.indexOf(u8, text, "Access denied") != null or
        std.mem.indexOf(u8, text, "store owner") != null or
        std.mem.indexOf(u8, text, "identity mismatch") != null;
}

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
    const scope = model.scope;
    if (!scope.hasValidTransport()) {
        if (model.pending) {
            fx.cancel(model.request);
            fx.cancelTimer(model.request);
            model.pending = false;
        }
        if (model.receipt_pending) {
            fx.cancel(model.receipt_request);
            fx.cancelTimer(model.receipt_request);
        }
        model.clearData("No local company connection configured.", .disconnected);
        return;
    }

    // Company, store, or socket switch ALWAYS wipes prior Data State & cancels in-flight
    // requests immediately, even before initial data has loaded.
    const scope_changed = (model.available and !model.scopeMatchesData()) or
        ((model.pending or model.receipt_pending or model.pending_command != null) and !model.scopeMatchesInFlight());
    if (scope_changed) {
        if (model.pending) {
            fx.cancel(model.request);
            fx.cancelTimer(model.request);
            model.pending = false;
        }
        if (model.receipt_pending) {
            fx.cancel(model.receipt_request);
            fx.cancelTimer(model.receipt_request);
        }
        model.clearData("Switching company scope…", .connecting);
    } else if (model.pending) {
        return;
    } else if (model.available) {
        model.link_state = .connecting;
        model.controls_locked = true;
        model.show_stale_banner = true;
        model.stale_banner = "Refreshing verified snapshot… Controls locked.";
    } else {
        model.link_state = .connecting;
        model.controls_locked = true;
        model.status = "Checking current access…";
    }

    model.stampInFlightScope();

    // Cold-start / post-switch recovery: reload any persisted pending command for this company
    // before unlocking controls.
    if (!model.pending_recovered and scope.pending_dir.len > 0 and model.pending_command == null) {
        var threaded: std.Io.Threaded = .init_single_threaded;
        model.recoverPendingCommand(threaded.io(), fx) catch {
            model.controls_locked = true;
            model.command_state = .unknown;
            model.show_command_status = true;
            model.command_status = "Saved pending command is unreadable; controls remain locked.";
        };
    }

    model.request = model.nextKey();
    model.pending = true;
    Transport.inspectWork(scope, model.request, fx);
}

pub fn update(model: *Model, msg: Msg, fx: *Effects) void {
    switch (msg) {
        .activate => read(model, fx),
        .reply => |result| {
            if (!model.pending or result.key != model.request) return;
            model.pending = false;
            fx.cancelTimer(result.key);
            if (result.reason != .exited or result.code != 0 or result.output_truncated) {
                if (result.reason == .exited and result.code != 0 and
                    (isAccessDenialOutput(result.stderr_tail) or isAccessDenialOutput(result.output)))
                {
                    if (model.receipt_pending) {
                        fx.cancel(model.receipt_request);
                        fx.cancelTimer(model.receipt_request);
                    }
                    model.clearData("Access denied: prior company data cleared.", .incompatible);
                    return;
                }
                model.markTransientDisconnect("Disconnected · Showing last verified snapshot · Controls locked.");
                return;
            }
            model.present(result.output) catch |err| switch (err) {
                error.IncompatibleTimeline => {
                    if (model.receipt_pending) {
                        fx.cancel(model.receipt_request);
                        fx.cancelTimer(model.receipt_request);
                    }
                    model.clearData(
                        "Company work unavailable: incompatible company or contract.",
                        .incompatible,
                    );
                },
                else => model.markTransientDisconnect(
                    "Stale · Incomplete response received · Controls locked.",
                ),
            };
        },
        .deadline => |timer| {
            if (!model.pending or timer.key != model.request) return;
            model.pending = false;
            fx.cancel(timer.key);
            model.markTransientDisconnect("Disconnected · Response timed out · Controls locked.");
        },
        .receipt_reply => |result| {
            if (!model.receipt_pending or result.key != model.receipt_request) return;
            model.receipt_pending = false;
            fx.cancelTimer(result.key);
            if (result.reason != .exited or result.code != 0 or result.output_truncated) {
                model.command_state = .unknown;
                model.controls_locked = true;
                model.command_status = "Unable to verify pending command receipt; controls remain locked.";
                return;
            }
            var threaded: std.Io.Threaded = .init_single_threaded;
            model.applyReceiptOutput(threaded.io(), result.output);
        },
        .receipt_deadline => |timer| {
            if (!model.receipt_pending or timer.key != model.receipt_request) return;
            model.receipt_pending = false;
            fx.cancel(timer.key);
            model.command_state = .unknown;
            model.controls_locked = true;
            model.command_status = "Receipt lookup timed out; controls remain locked.";
        },
    }
}

pub fn tokens(_: *const Model) sdk.canvas.DesignTokens {
    return yogurt.theme(false, .neutral, false, false);
}

test "a compatible response reports missing observations without substituting zero" {
    var model = Model{ .scope = .{ .company = "company", .store = "/tmp/company.sqlite" } };
    defer model.deinit();
    const body =
        \\{"contract_revision":"work-timeline-v1","company_id":"company","revision":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","as_of_ns":1700000000000000000,"duties":[{"duty_id":"d","owner_id":"owner","state":"due","due_at":null,"decision":null}],"paper":null}
    ;
    try model.present(body);
    try std.testing.expectEqualStrings("Not recorded", model.duties[0].decision);
    try std.testing.expectEqualStrings("2023-11-14 22:13:20 UTC", model.observed);
    try std.testing.expectEqualStrings("No investment observations in this view.", model.investment);
    try std.testing.expect(!model.show_risk_control);
    try std.testing.expectEqual(LinkState.connected, model.link_state);
    try std.testing.expect(!model.controls_locked);
    model.clearData("Unavailable", .disconnected);
    model.scope.company = "other";
    try std.testing.expectError(error.IncompatibleTimeline, model.present(body));
    try std.testing.expect(!model.available and model.duties.len == 0);
}

test "transient disconnect and routine refresh preserve verified Data State and lock controls" {
    var model = Model{
        .scope = .{
            .executable = "/usr/bin/true",
            .store = "/tmp/company.sqlite",
            .company = "company",
        },
    };
    defer model.deinit();
    const body =
        \\{"contract_revision":"work-timeline-v1","company_id":"company","revision":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","as_of_ns":1700000000000000000,"duties":[{"duty_id":"d1","owner_id":"owner","state":"due","due_at":null,"decision":null}],"paper":null,"risk_control":{"state":"suspended","effective_revision":3}}
    ;
    try model.present(body);
    try std.testing.expect(model.available and !model.stale and !model.controls_locked);

    var fx_arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer fx_arena.deinit();
    var fx: Effects = .{ .allocator = fx_arena.allocator() };
    // Routine refresh keeps last verified duties visible while locking controls
    read(&model, &fx);
    try std.testing.expect(model.available);
    try std.testing.expectEqual(@as(usize, 1), model.duties.len);
    try std.testing.expectEqual(LinkState.connecting, model.link_state);
    try std.testing.expect(model.controls_locked);
    try std.testing.expect(model.show_stale_banner);

    // Truncated code==0 response containing "403" is treated as transient disconnect, not false-positive access denial
    update(&model, .{ .reply = .{ .key = model.request, .code = 0, .output = "403-inside-json", .output_truncated = true } }, &fx);
    try std.testing.expect(model.available);
    try std.testing.expect(model.stale);
    try std.testing.expectEqual(LinkState.disconnected, model.link_state);
    try std.testing.expect(model.controls_locked);

    // Timeout marks stale/disconnected WITHOUT blank-wiping duties or risk_control
    read(&model, &fx);
    update(&model, .{ .deadline = .{ .key = model.request } }, &fx);
    try std.testing.expect(model.available);
    try std.testing.expect(model.stale);
    try std.testing.expectEqual(LinkState.disconnected, model.link_state);
    try std.testing.expect(model.controls_locked);
    try std.testing.expectEqualStrings("d1", model.duties[0].duty_id);
    try std.testing.expectEqualStrings("New paper risk is suspended · Control revision 3", model.risk_control);

    // Switching company_id wipes cached Data State immediately even while refresh is pending
    read(&model, &fx);
    try std.testing.expect(model.pending);
    model.scope.company = "other-company";
    read(&model, &fx);
    try std.testing.expect(!model.available);
    try std.testing.expectEqual(@as(usize, 0), model.duties.len);
}

test "access denial clears cached private data and in-memory pending state immediately" {
    const io = std.testing.io;
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    const root_dir = try tmp.dir.realPathFileAlloc(io, ".", std.testing.allocator);
    defer std.testing.allocator.free(root_dir);
    const pending_dir = try std.fmt.allocPrint(std.testing.allocator, "{s}/board-pending", .{root_dir});
    defer std.testing.allocator.free(pending_dir);

    var model = Model{
        .scope = .{
            .executable = "/usr/bin/true",
            .store = "/tmp/company.sqlite",
            .company = "company",
            .pending_dir = pending_dir,
        },
    };
    defer model.deinit();
    const body =
        \\{"contract_revision":"work-timeline-v1","company_id":"company","revision":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","as_of_ns":1700000000000000000,"duties":[{"duty_id":"d1","owner_id":"owner","state":"due","due_at":null,"decision":null}],"paper":null}
    ;
    try model.present(body);
    try model.stagePendingCommand(io, "cmd-secret", "suspend_new_risk", "{\"reason\":\"private\"}", 0, 1700000000_000000000);
    var fx_arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer fx_arena.deinit();
    var fx: Effects = .{ .allocator = fx_arena.allocator() };
    read(&model, &fx);
    // Transient stderr containing numeric digits like "4031" must NOT false-wipe Data State.
    update(&model, .{ .reply = .{ .key = model.request, .code = 1, .stderr_tail = "transient disconnect on pid 4031" } }, &fx);
    try std.testing.expect(model.available);
    try std.testing.expect(model.stale);
    try std.testing.expect(model.controls_locked);

    // Real CLI stderr access denial ("HTTP 403") immediately wipes cached private data & pending state.
    read(&model, &fx);
    update(&model, .{ .reply = .{ .key = model.request, .code = 1, .stderr_tail = "Company command refused: HTTP 403: Control plane company identity mismatch" } }, &fx);
    try std.testing.expect(!model.available);
    try std.testing.expectEqual(LinkState.incompatible, model.link_state);
    try std.testing.expectEqual(@as(usize, 0), model.duties.len);
    try std.testing.expect(model.pending_command == null);
    try std.testing.expect(!model.show_command_status);
    try std.testing.expect(model.controls_locked);
}

test "cold start read automatically recovers pending command with disjoint keys and validates receipt" {
    const io = std.testing.io;
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    const root_dir = try tmp.dir.realPathFileAlloc(io, ".", std.testing.allocator);
    defer std.testing.allocator.free(root_dir);
    const pending_dir = try std.fmt.allocPrint(std.testing.allocator, "{s}/board-pending", .{root_dir});
    defer std.testing.allocator.free(pending_dir);

    var model = Model{
        .scope = .{
            .executable = "/usr/bin/true",
            .store = "/tmp/company.sqlite",
            .company = "company-1",
            .pending_dir = pending_dir,
        },
    };
    defer model.deinit();
    const body =
        \\{"contract_revision":"work-timeline-v1","company_id":"company-1","revision":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","as_of_ns":1700000000000000000,"duties":[],"paper":null}
    ;
    try model.present(body);
    try model.stagePendingCommand(io, "cmd-fixed-id", "suspend_new_risk", "{\"reason\":\"check\"}", 0, 1700000000_000000000);
    try std.testing.expectEqual(pending_store.CommandLifecycle.submitting, model.command_state);
    try std.testing.expect(model.controls_locked);

    // Cold restart invokes read() directly (init_fx) -> recovers exact command_id with disjoint effect keys!
    var restarted = Model{
        .scope = .{
            .executable = "/usr/bin/true",
            .store = "/tmp/company.sqlite",
            .company = "company-1",
            .pending_dir = pending_dir,
        },
    };
    defer restarted.deinit();
    var fx_arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer fx_arena.deinit();
    var fx: Effects = .{ .allocator = fx_arena.allocator() };
    read(&restarted, &fx);
    try std.testing.expectEqual(pending_store.CommandLifecycle.unknown, restarted.command_state);
    try std.testing.expectEqualStrings("cmd-fixed-id", restarted.pending_command.?.command_id);
    try std.testing.expect(restarted.request != restarted.receipt_request);
    try std.testing.expect(restarted.controls_locked);

    // Mismatched command_type or unknown status/state fail closed and keep controls locked
    restarted.applyReceiptOutput(io, "{\"status\":\"found\",\"company_id\":\"company-1\",\"command_id\":\"cmd-fixed-id\",\"command_type\":\"resume_new_risk\",\"state\":\"applied\"}");
    try std.testing.expectEqual(pending_store.CommandLifecycle.unknown, restarted.command_state);
    try std.testing.expect(restarted.pending_command != null);
    restarted.applyReceiptOutput(io, "{\"status\":\"bogus\",\"company_id\":\"company-1\",\"command_id\":\"cmd-fixed-id\",\"command_type\":\"suspend_new_risk\",\"state\":\"applied\"}");
    try std.testing.expectEqual(pending_store.CommandLifecycle.unknown, restarted.command_state);
    restarted.applyReceiptOutput(io, "{\"status\":\"found\",\"company_id\":\"company-1\",\"command_id\":\"cmd-fixed-id\",\"command_type\":\"suspend_new_risk\",\"state\":\"mystery\"}");
    try std.testing.expectEqual(pending_store.CommandLifecycle.unknown, restarted.command_state);

    // 404 not_found offers explicit retry-same-ID vs discard without generating a new ID
    restarted.applyReceiptOutput(io, "{\"status\":\"not_found\",\"company_id\":\"company-1\",\"command_id\":\"cmd-fixed-id\"}");
    try std.testing.expectEqual(pending_store.CommandLifecycle.not_found_retry_choice, restarted.command_state);
    const retried = try restarted.retryPendingSameId();
    try std.testing.expectEqualStrings("cmd-fixed-id", retried.command_id);

    // Applied receipt converges, removes pending file, and stays locked until Data State is verified
    restarted.applyReceiptOutput(io, "{\"status\":\"found\",\"company_id\":\"company-1\",\"command_id\":\"cmd-fixed-id\",\"command_type\":\"suspend_new_risk\",\"state\":\"applied\"}");
    try std.testing.expectEqual(pending_store.CommandLifecycle.applied, restarted.command_state);
    try std.testing.expect(restarted.pending_command == null);
    try std.testing.expect(restarted.controls_locked);
    try restarted.present(body);
    try std.testing.expect(!restarted.controls_locked);

    var check_arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer check_arena.deinit();
    try std.testing.expect((try pending_store.loadPending(io, check_arena.allocator(), pending_dir, "company-1")) == null);
}

test "an old or incomplete reply cannot restore company work" {
    var model = Model{ .request = 2, .pending = true };
    defer model.deinit();
    var fx_arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer fx_arena.deinit();
    var fx: Effects = .{ .allocator = fx_arena.allocator() };
    update(&model, .{ .reply = .{ .key = 1, .code = 0, .output = "old response" } }, &fx);
    try std.testing.expect(model.pending and !model.available);
    update(&model, .{ .reply = .{ .key = 2, .code = 0, .output_truncated = true } }, &fx);
    try std.testing.expect(!model.pending and !model.available and model.duties.len == 0);
}

test "uds socket transport works without a direct sqlite store path and clears on socket switch" {
    var model = Model{
        .scope = .{
            .executable = "/usr/bin/true",
            .socket = "/tmp/sourdough-a.sock",
            .company = "company-1",
        },
    };
    defer model.deinit();
    try std.testing.expect(model.scope.hasValidTransport());

    var fx_arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer fx_arena.deinit();
    var fx: Effects = .{ .allocator = fx_arena.allocator() };
    read(&model, &fx);
    try std.testing.expect(model.pending);
    try std.testing.expectEqual(LinkState.connecting, model.link_state);

    const body =
        \\{"contract_revision":"work-timeline-v1","company_id":"company-1","revision":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","as_of_ns":1700000000000000000,"duties":[],"paper":null}
    ;
    update(&model, .{ .reply = .{ .key = model.request, .code = 0, .output = body } }, &fx);
    try std.testing.expect(model.available);
    try std.testing.expectEqual(LinkState.connected, model.link_state);
    try std.testing.expect(!model.controls_locked);

    // Switching daemon socket clears cached Data State immediately even if store_key matches.
    model.scope.store_key = "1:2";
    try model.present(body);
    model.scope.socket = "/tmp/sourdough-b.sock";
    read(&model, &fx);
    try std.testing.expect(!model.available);
    try std.testing.expect(model.controls_locked);
    try std.testing.expectEqual(LinkState.connecting, model.link_state);

    // Switching daemon socket while request is still in flight (available == false)
    // cancels the old request key and dispatches a fresh one.
    const old_key = model.request;
    model.scope.socket = "/tmp/sourdough-c.sock";
    read(&model, &fx);
    try std.testing.expect(model.pending);
    try std.testing.expect(model.request != old_key);
}
