"""Design tokens adapted from the Figma main bookshelf frame."""

from __future__ import annotations


class Theme:
    # Window and page layout
    window_width = 1200
    window_height = 860
    sidebar_width = 260
    toolbar_height = 52
    content_padding = 36
    content_top_padding = 15
    book_card_width = 190
    book_card_height = 340
    book_card_radius = 10
    cover_width = 170
    cover_height = 241
    cover_radius = 6
    grid_gap = 25
    toolbar_control_height = 36
    search_width = 200
    search_panel_width = 242
    file_filter_width = 90
    sort_dropdown_width = 146
    control_radius = 10
    control_border_width = 1
    control_visual_padding = 4
    control_layout_margin = control_visual_padding - control_border_width
    control_gap = 6
    control_text_height = 16
    body_font_size = 16
    dropdown_inner_size = 28
    search_bar_gap = 4
    search_input_text_width = 141
    search_input_height = 24
    search_inner_button_size = 28
    search_inner_button_radius = 6

    # Frameless window chrome and toolbar controls
    traffic_light_size = 14
    traffic_light_group_width = 62
    traffic_light_group_height = 16
    chrome_button_size = 36
    action_button_width = 42
    action_button_height = 36
    action_button_indicator_size = 10
    icon_size = 24

    # Figma two-option switches
    switch_width = 70
    switch_height = 36
    switch_border_width = 2
    switch_visual_padding = 4
    switch_layout_margin = switch_visual_padding - switch_border_width
    switch_gap = 6
    switch_option_size = 28
    switch_option_radius = 6
    switch_option_border_width = 1
    switch_option_icon_inset = (switch_option_size - icon_size) // 2

    # Reusable fixed control sizes
    toolbar_button_size = 36
    card_button_size = 28
    sidebar_item_height = 28
    sidebar_section_height = 34
    toolbar_spacer_width = 2
    resize_grip_size = 16
    tester_reset_width = 96
    tester_reset_height = 36

    # Figma menu popups and native combo popup metrics
    menu_width = 130
    menu_border_width = 2
    menu_radius = 10
    menu_visual_padding = 6
    menu_layout_margin = menu_visual_padding - menu_border_width
    menu_option_gap = 2
    menu_item_radius = 4
    menu_item_padding = 2
    menu_item_text_gap = 10
    menu_item_text_height = 15
    menu_item_height = menu_item_text_height + (menu_item_padding * 2)
    menu_font_size = 12

    # Palette
    color_window = "#ffffff"
    color_content = "#f5f5f5"
    color_selected = "#e5e5e5"
    color_button_edge = "#929292"
    color_button_inner_edge = "#e0e0e0"
    color_menu_background_rgba = (255, 255, 255, 204)
    color_menu_item_hover_rgba = (220, 220, 220, 102)
    color_menu_destructive = "#bf0c0c"
    color_switch_background = "#bfbfbf"
    color_progress_background = "#c9c9c9"
    color_progress_fill = "#8a8a8a"
    color_sidebar_section = "#6d6d6d"
    color_text = "#000000"
    color_text_muted = "#6d6d6d"

    # General spacing scale
    spacing_xs = 4
    spacing_sm = 8
    spacing_md = 10
    spacing_lg = 16
    spacing_xl = 24

    @classmethod
    def qss_tokens(cls) -> dict[str, str]:
        """Theme values that QSS needs without duplicating literals."""

        return {
            "__BUTTON_INNER_EDGE__": cls.color_button_inner_edge,
            "__BUTTON_EDGE__": cls.color_button_edge,
            "__WINDOW_COLOR__": cls.color_window,
            "__SELECTED_COLOR__": cls.color_selected,
            "__CONTROL_RADIUS__": f"{cls.control_radius}px",
            "__SEARCH_INNER_RADIUS__": f"{cls.search_inner_button_radius}px",
            "__BODY_FONT_SIZE__": f"{cls.body_font_size}px",
            "__MENU_BACKGROUND__": cls._rgba_qss(cls.color_menu_background_rgba),
            "__MENU_BORDER_WIDTH__": f"{cls.menu_border_width}px",
            "__MENU_RADIUS__": f"{cls.menu_radius}px",
            "__MENU_LAYOUT_MARGIN__": f"{cls.menu_layout_margin}px",
            "__MENU_ITEM_RADIUS__": f"{cls.menu_item_radius}px",
            "__MENU_ITEM_PADDING__": f"{cls.menu_item_padding}px",
            "__MENU_ITEM_HEIGHT__": f"{cls.menu_item_height}px",
            "__MENU_ITEM_HOVER__": cls._rgba_qss(cls.color_menu_item_hover_rgba),
            "__MENU_DESTRUCTIVE__": cls.color_menu_destructive,
            "__MENU_FONT_SIZE__": f"{cls.menu_font_size}px",
            "__TEXT_COLOR__": cls.color_text,
        }

    @staticmethod
    def _rgba_qss(value: tuple[int, int, int, int]) -> str:
        return f"rgba({value[0]}, {value[1]}, {value[2]}, {value[3]})"
