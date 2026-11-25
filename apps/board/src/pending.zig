const std = @import("std");

pub const PendingCommand = struct {
    command_id: []const u8,
    command_type: []const u8,
    payload: []const u8,
    payload_sha256: []const u8,
    expected_revision: u64,
    sent_ns: i64,
};

pub const CommandLifecycle = enum {
    idle,
    draft,
    submitting,
    unknown,
    accepted,
    applied,
    denied,
    not_found_retry_choice,
};

pub const ReceiptStatus = struct {
    status: []const u8,
    company_id: []const u8,
    command_id: []const u8,
    command_type: ?[]const u8 = null,
    state: ?[]const u8 = null,
    denial_code: ?[]const u8 = null,
};

pub fn isSafeCompanyId(company_id: []const u8) bool {
    if (company_id.len == 0 or company_id.len > 128) return false;
    if (company_id[0] == '.') return false;
    if (std.mem.indexOf(u8, company_id, "..") != null) return false;
    for (company_id) |ch| {
        const ok = (ch >= 'a' and ch <= 'z') or
            (ch >= 'A' and ch <= 'Z') or
            (ch >= '0' and ch <= '9') or
            ch == '.' or ch == '_' or ch == '-';
        if (!ok) return false;
    }
    return true;
}

pub fn payloadSha256Hex(payload: []const u8) [64]u8 {
    var hasher = std.crypto.hash.sha2.Sha256.init(.{});
    hasher.update(payload);
    const digest = hasher.finalResult();
    return std.fmt.bytesToHex(digest, .lower);
}

pub fn defaultPendingDir(alloc: std.mem.Allocator, home: []const u8) ![]u8 {
    if (!std.fs.path.isAbsolute(home)) return error.InvalidHomeDirectory;
    return std.fmt.allocPrint(alloc, "{s}/Library/Application Support/Sourdough/board-pending", .{home});
}

pub fn pendingFilePath(
    alloc: std.mem.Allocator,
    base_dir: []const u8,
    company_id: []const u8,
) ![]u8 {
    if (!std.fs.path.isAbsolute(base_dir)) return error.InvalidPendingDirectory;
    if (!isSafeCompanyId(company_id)) return error.InvalidCompanyId;
    return std.fmt.allocPrint(alloc, "{s}/{s}.json", .{ base_dir, company_id });
}

fn openPrivateDir(alloc: std.mem.Allocator, dir_path: []const u8) !std.c.fd_t {
    const zdir = try alloc.dupeZ(u8, dir_path);
    defer alloc.free(zdir);
    const fd = std.c.open(
        zdir.ptr,
        .{ .ACCMODE = .RDONLY, .DIRECTORY = true, .NOFOLLOW = true },
        @as(std.c.mode_t, 0),
    );
    if (fd < 0) return error.OpenDirFailed;
    errdefer _ = std.c.close(fd);
    if (std.c.fchmod(fd, @as(std.c.mode_t, 0o700)) != 0) return error.ChmodFailed;
    return fd;
}

fn writeAllFd(fd: std.c.fd_t, data: []const u8) !void {
    var offset: usize = 0;
    while (offset < data.len) {
        const rc = std.c.write(fd, data.ptr + offset, data.len - offset);
        if (rc <= 0) return error.WriteFailed;
        offset += @intCast(rc);
    }
}

pub fn savePendingAtomic(
    io: std.Io,
    alloc: std.mem.Allocator,
    base_dir: []const u8,
    company_id: []const u8,
    cmd: PendingCommand,
) !void {
    const expected_hex = payloadSha256Hex(cmd.payload);
    if (!std.mem.eql(u8, cmd.payload_sha256, &expected_hex)) {
        return error.PayloadDigestMismatch;
    }
    if (cmd.command_id.len == 0 or cmd.command_type.len == 0 or cmd.sent_ns <= 0) {
        return error.InvalidPendingCommand;
    }
    const target_path = try pendingFilePath(alloc, base_dir, company_id);
    defer alloc.free(target_path);

    const cwd = std.Io.Dir.cwd();
    try cwd.createDirPath(io, base_dir);
    const dir_fd = try openPrivateDir(alloc, base_dir);
    defer _ = std.c.close(dir_fd);

    const encoded = try std.json.Stringify.valueAlloc(alloc, cmd, .{});
    defer alloc.free(encoded);

    const tmp_path = try std.fmt.allocPrint(
        alloc,
        "{s}/.{s}.{d}.tmp",
        .{ base_dir, company_id, cmd.sent_ns },
    );
    defer alloc.free(tmp_path);
    const z_tmp = try alloc.dupeZ(u8, tmp_path);
    defer alloc.free(z_tmp);
    const z_target = try alloc.dupeZ(u8, target_path);
    defer alloc.free(z_target);

    // Create strictly private (0o600) exclusive file without following symlinks.
    _ = std.c.unlink(z_tmp.ptr);
    const fd = std.c.open(
        z_tmp.ptr,
        .{ .ACCMODE = .WRONLY, .CREAT = true, .EXCL = true, .NOFOLLOW = true },
        @as(std.c.mode_t, 0o600),
    );
    if (fd < 0) return error.CreatePendingFailed;
    var closed = false;
    errdefer {
        if (!closed) _ = std.c.close(fd);
        _ = std.c.unlink(z_tmp.ptr);
    }
    if (std.c.fchmod(fd, @as(std.c.mode_t, 0o600)) != 0) return error.ChmodFailed;
    try writeAllFd(fd, encoded);
    if (std.c.fsync(fd) != 0) return error.FsyncFailed;
    _ = std.c.close(fd);
    closed = true;

    if (std.c.rename(z_tmp.ptr, z_target.ptr) != 0) return error.RenamePendingFailed;
    if (std.c.fsync(dir_fd) != 0) return error.FsyncFailed;
}

pub fn loadPending(
    io: std.Io,
    alloc: std.mem.Allocator,
    base_dir: []const u8,
    company_id: []const u8,
) !?PendingCommand {
    const target_path = try pendingFilePath(alloc, base_dir, company_id);
    defer alloc.free(target_path);

    const raw = std.Io.Dir.cwd().readFileAlloc(io, target_path, alloc, .limited(64 * 1024)) catch |err| switch (err) {
        error.FileNotFound => return null,
        else => return err,
    };
    defer alloc.free(raw);
    const parsed = std.json.parseFromSliceLeaky(PendingCommand, alloc, raw, .{
        .ignore_unknown_fields = false,
        .allocate = .alloc_always,
    }) catch return error.CorruptedPendingCommand;
    const expected_hex = payloadSha256Hex(parsed.payload);
    if (parsed.command_id.len == 0 or
        parsed.command_type.len == 0 or
        parsed.sent_ns <= 0 or
        !std.mem.eql(u8, parsed.payload_sha256, &expected_hex))
    {
        return error.CorruptedPendingCommand;
    }
    return parsed;
}

pub fn clearPending(
    io: std.Io,
    alloc: std.mem.Allocator,
    base_dir: []const u8,
    company_id: []const u8,
) !void {
    const target_path = try pendingFilePath(alloc, base_dir, company_id);
    defer alloc.free(target_path);
    std.Io.Dir.cwd().deleteFile(io, target_path) catch |err| switch (err) {
        error.FileNotFound => {},
        else => return err,
    };
    const dir_fd = openPrivateDir(alloc, base_dir) catch return;
    defer _ = std.c.close(dir_fd);
    _ = std.c.fsync(dir_fd);
}

pub fn parseReceiptStatus(alloc: std.mem.Allocator, body: []const u8) !ReceiptStatus {
    return std.json.parseFromSliceLeaky(ReceiptStatus, alloc, body, .{
        .ignore_unknown_fields = true,
        .allocate = .alloc_always,
    });
}

test "pending command persists atomically with 0o600 permissions and reloads cleanly" {
    const io = std.testing.io;
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const alloc = arena.allocator();

    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    const dir = try tmp.dir.realPathFileAlloc(io, ".", alloc);
    const pending_dir = try std.fmt.allocPrint(alloc, "{s}/board-pending", .{dir});

    const payload = "{\"reason\":\"Review open exposure\"}";
    const digest = payloadSha256Hex(payload);
    const cmd = PendingCommand{
        .command_id = "cmd-suspend-001",
        .command_type = "suspend_new_risk",
        .payload = payload,
        .payload_sha256 = &digest,
        .expected_revision = 2,
        .sent_ns = 1700000000_000000000,
    };

    try savePendingAtomic(io, alloc, pending_dir, "company-abc", cmd);
    const target = try pendingFilePath(alloc, pending_dir, "company-abc");
    const z_target = try alloc.dupeZ(u8, target);
    const check_fd = std.c.open(z_target.ptr, .{ .ACCMODE = .RDONLY, .NOFOLLOW = true }, @as(std.c.mode_t, 0));
    try std.testing.expect(check_fd >= 0);
    defer _ = std.c.close(check_fd);
    var st: std.c.Stat = undefined;
    try std.testing.expectEqual(@as(c_int, 0), std.c.fstat(check_fd, &st));
    try std.testing.expectEqual(@as(std.c.mode_t, 0o600), st.mode & 0o777);

    const reloaded = (try loadPending(io, alloc, pending_dir, "company-abc")) orelse return error.MissingPending;
    try std.testing.expectEqualStrings("cmd-suspend-001", reloaded.command_id);
    try std.testing.expectEqualStrings("suspend_new_risk", reloaded.command_type);
    try std.testing.expectEqualStrings(&digest, reloaded.payload_sha256);
    try std.testing.expectEqual(@as(u64, 2), reloaded.expected_revision);

    try clearPending(io, alloc, pending_dir, "company-abc");
    try std.testing.expect((try loadPending(io, alloc, pending_dir, "company-abc")) == null);
}

test "pending path rejects traversal, leading dot, and tampered payload digest" {
    const io = std.testing.io;
    var arena = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena.deinit();
    const alloc = arena.allocator();

    try std.testing.expectError(
        error.InvalidCompanyId,
        pendingFilePath(alloc, "/tmp/board-pending", "../escape"),
    );
    try std.testing.expectError(
        error.InvalidCompanyId,
        pendingFilePath(alloc, "/tmp/board-pending", ".hidden"),
    );
    const bad = PendingCommand{
        .command_id = "cmd-1",
        .command_type = "suspend_new_risk",
        .payload = "{\"reason\":\"x\"}",
        .payload_sha256 = "0000000000000000000000000000000000000000000000000000000000000000",
        .expected_revision = 0,
        .sent_ns = 100,
    };
    try std.testing.expectError(
        error.PayloadDigestMismatch,
        savePendingAtomic(io, alloc, "/tmp/board-pending", "company-1", bad),
    );
}
