const std = @import("std");
const runner = @import("runner");
const sdk = @import("native_sdk");
const model = @import("timeline.zig");
const yogurt = @import("yogurt/theme.zig");

pub const panic = std.debug.FullPanic(sdk.debug.capturePanic);
pub const Model = model.Model;
pub const Msg = model.Msg;

const canvas_label = "main-canvas";
const width = 680;
const height = 620;
const views = [_]sdk.ShellView{.{
    .label = canvas_label,
    .kind = .gpu_surface,
    .fill = true,
    .role = "Company work",
    .accessibility_label = "Company work",
    .gpu_backend = .metal,
    .gpu_pixel_format = .bgra8_unorm,
    .gpu_present_mode = .timer,
    .gpu_alpha_mode = .@"opaque",
    .gpu_color_space = .srgb,
    .gpu_vsync = true,
}};
const windows = [_]sdk.ShellWindow{.{
    .label = "main",
    .title = "Sourdough Board",
    .width = width,
    .height = height,
    .views = &views,
}};

pub fn main(init: std.process.Init) !void {
    const app = try sdk.UiApp(Model, Msg).create(std.heap.page_allocator, .{
        .name = "sourdough-board",
        .scene = .{ .windows = &windows },
        .canvas_label = canvas_label,
        .init_fx = model.read,
        .update_fx = model.update,
        .tokens_fn = model.tokens,
        .fonts = &.{
            .{ .id = yogurt.font_id, .name = "Rethink Sans", .ttf = @embedFile("yogurt/rethink-sans.ttf") },
            .{ .id = yogurt.button_font_id, .name = "Rethink Sans Medium", .ttf = @embedFile("yogurt/rethink-sans-medium.ttf") },
        },
        .markup = .{ .source = @embedFile("app.native"), .sources = &.{@import("yogurt/button.zig").file} },
    });
    defer app.destroy();
    app.model.scope = .{
        .executable = init.environ_map.get("SOURDOUGH_SERVICE") orelse "",
        .store = init.environ_map.get("SOURDOUGH_STORE") orelse "",
        .company = init.environ_map.get("SOURDOUGH_COMPANY") orelse "",
    };
    defer app.model.storage.deinit();
    try runner.runWithOptions(app.app(), .{
        .app_name = "sourdough-board",
        .window_title = "Sourdough Board",
        .bundle_id = "dev.openboa.sourdough.board",
        .icon_path = "assets/sourdough.svg",
        .default_frame = sdk.geometry.RectF.init(0, 0, width, height),
        .js_window_api = false,
        .security = .{ .permissions = &.{sdk.security.permission_view} },
    }, init);
}

test {
    _ = @import("timeline.zig");
}
