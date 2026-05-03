"""Design tokens adapted from the Figma main bookshelf frame."""

from __future__ import annotations


class Theme:
    # Window and page layout
    window_width = 1200
    window_height = 860
    window_min_width = 900
    window_min_height = 600
    window_corner_radius = 18
    sidebar_width = 260
    toolbar_height = 52
    content_frame_width = 934
    content_frame_height = 808
    content_min_width = 719
    content_horizontal_padding = 36
    content_padding = content_horizontal_padding
    content_top_padding = 15
    content_radius = 10
    banner_horizontal_padding = 2
    book_card_width = 190
    book_card_height = 340
    book_card_radius = 10
    book_selection_border_width = 2
    book_card_padding = 10
    book_card_layout_margin = book_card_padding - book_selection_border_width
    book_card_gap = 8
    cover_width = 170
    cover_height = 241
    cover_radius = 6
    book_list_row_width = 598
    book_list_row_height = 120
    book_list_cover_width = 71
    book_list_cover_height = 100
    book_list_content_padding_horizontal = 2
    book_control_bar_height = 32
    book_control_bar_padding = 2
    book_option_frame_gap = 4
    book_progress_width = 65
    book_progress_height = 10
    book_progress_radius = 5
    book_progress_percent_gap = 10

    # Floating book detail panel from Figma node 162:1473 / component 162:861.
    detail_panel_top_margin = 81
    detail_panel_horizontal_margin = 29
    detail_panel_border_width = 2
    detail_panel_radius = 10
    detail_panel_visual_padding = 8
    detail_panel_layout_margin = detail_panel_visual_padding - detail_panel_border_width
    detail_panel_gap = 10
    detail_description_padding_horizontal = 50
    detail_description_padding_vertical = 40
    detail_description_gap = 10
    detail_cover_width = 200
    detail_cover_height = 284
    detail_cover_panel_gap = 10
    detail_cover_panel_bottom_padding = 10
    detail_progress_width = 120
    detail_progress_unit_width = 160
    detail_content_padding = 4
    detail_meta_height = 252
    detail_meta_name_gap = 10
    detail_attribute_padding_horizontal = 6
    detail_attribute_gap = 10
    detail_attribute_border_width = 1
    detail_attribute_visual_padding = 4
    detail_attribute_layout_margin = detail_attribute_visual_padding - detail_attribute_border_width
    detail_attribute_radius = 6
    detail_control_padding = 6
    detail_control_gap = 10
    detail_read_button_width = 100
    detail_button_size = 36
    detail_button_border_width = 1
    detail_button_visual_padding = 4
    detail_button_layout_margin = detail_button_visual_padding - detail_button_border_width
    detail_button_radius = 10
    detail_title_font_size = 20
    detail_meta_font_size = 14
    detail_read_font_size = 15
    detail_thumbnail_width = 100
    detail_thumbnail_height = 142
    detail_thumbnail_radius = 6
    detail_thumbnail_frame_padding = 10
    detail_thumbnail_row_padding_horizontal = 10
    detail_thumbnail_row_padding_vertical = 5
    detail_thumbnail_gap = 20
    detail_thumbnail_row_gap = 10
    detail_thumbnail_min_width = 625
    grid_min_gap = 20
    grid_gap = grid_min_gap
    grid_top_padding = 4
    grid_bottom_padding = 24
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
    card_button_radius = 6

    # Sidebar frame from Figma node 165:2982
    # The root uses px=4/py=10, then each section/item stretches to the
    # available inner width. Keep these in Python because they define layout
    # geometry rather than visual paint.
    sidebar_margin_horizontal = 4
    sidebar_margin_vertical = 10
    sidebar_gap = 4
    sidebar_item_height = 28
    sidebar_section_height = 34
    sidebar_section_padding_left = 15
    sidebar_section_padding_right = 10
    sidebar_section_padding_top = 10
    sidebar_section_padding_bottom = 5
    sidebar_section_arrow_size = 20
    sidebar_item_padding_left = 15
    sidebar_item_padding_right = 10
    sidebar_item_padding_vertical = 2
    sidebar_item_icon_text_gap = 5
    sidebar_item_radius = 6
    sidebar_item_font_size = 14
    sidebar_section_font_size = 16
    sidebar_lower_padding_bottom = 5
    toolbar_spacer_width = 2
    resize_grip_size = 16
    tester_reset_width = 96
    tester_reset_height = 36

    # Floating shelf scrollbars
    shelf_scrollbar_width = 10
    shelf_scrollbar_radius = 5
    shelf_scrollbar_min_height = 40
    shelf_scrollbar_bottom_margin = 18
    scrollbar_handle_hide_delay_ms = 900
    # Qt scrollbars consume viewport width. Reducing the content right margin
    # keeps the visual gap from content to app edge at Figma's 48px.
    content_scrollbar_adjusted_right_padding = content_horizontal_padding - shelf_scrollbar_width

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
    color_card_selected = "#727272"
    color_card_selected_transparent_rgba = (114, 114, 114, 0)
    color_scrollbar_handle_hidden_rgba = (138, 138, 138, 0)
    color_scrollbar_handle_rgba = (138, 138, 138, 160)
    color_scrollbar_handle_hover_rgba = (138, 138, 138, 220)
    color_sidebar_section = "#6d6d6d"
    color_sidebar_item_hover = "#eeeeee"
    color_text = "#000000"
    color_text_muted = "#6d6d6d"
    missing_book_opacity = 0.6

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
            "__WINDOW_RADIUS__": f"{cls.window_corner_radius}px",
            "__CONTENT_COLOR__": cls.color_content,
            "__SELECTED_COLOR__": cls.color_selected,
            "__CONTENT_RADIUS__": f"{cls.content_radius}px",
            "__SIDEBAR_SECTION_COLOR__": cls.color_sidebar_section,
            "__SIDEBAR_ITEM_RADIUS__": f"{cls.sidebar_item_radius}px",
            "__SIDEBAR_ITEM_HOVER__": cls.color_sidebar_item_hover,
            "__SIDEBAR_ITEM_FONT_SIZE__": f"{cls.sidebar_item_font_size}px",
            "__SIDEBAR_SECTION_FONT_SIZE__": f"{cls.sidebar_section_font_size}px",
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
            "__TEXT_MUTED_COLOR__": cls.color_text_muted,
            "__BOOK_CARD_RADIUS__": f"{cls.book_card_radius}px",
            "__BOOK_COVER_RADIUS__": f"{cls.cover_radius}px",
            "__CARD_BUTTON_RADIUS__": f"{cls.card_button_radius}px",
            "__CARD_SELECTED__": cls.color_card_selected,
            "__CARD_SELECTED_TRANSPARENT__": cls._rgba_qss(cls.color_card_selected_transparent_rgba),
            "__BOOK_SELECTION_BORDER_WIDTH__": f"{cls.book_selection_border_width}px",
            "__PROGRESS_BACKGROUND__": cls.color_progress_background,
            "__PROGRESS_FILL__": cls.color_progress_fill,
            "__PROGRESS_RADIUS__": f"{cls.book_progress_radius}px",
            "__DETAIL_PANEL_BACKGROUND__": cls._rgba_qss(cls.color_menu_background_rgba),
            "__DETAIL_PANEL_BORDER_WIDTH__": f"{cls.detail_panel_border_width}px",
            "__DETAIL_PANEL_RADIUS__": f"{cls.detail_panel_radius}px",
            "__DETAIL_ATTRIBUTE_RADIUS__": f"{cls.detail_attribute_radius}px",
            "__DETAIL_BUTTON_BORDER_WIDTH__": f"{cls.detail_button_border_width}px",
            "__DETAIL_BUTTON_LAYOUT_MARGIN__": f"{cls.detail_button_layout_margin}px",
            "__DETAIL_BUTTON_RADIUS__": f"{cls.detail_button_radius}px",
            "__DETAIL_TITLE_FONT_SIZE__": f"{cls.detail_title_font_size}px",
            "__DETAIL_META_FONT_SIZE__": f"{cls.detail_meta_font_size}px",
            "__DETAIL_READ_FONT_SIZE__": f"{cls.detail_read_font_size}px",
            "__SHELF_SCROLLBAR_WIDTH__": f"{cls.shelf_scrollbar_width}px",
            "__SHELF_SCROLLBAR_RADIUS__": f"{cls.shelf_scrollbar_radius}px",
            "__SHELF_SCROLLBAR_MIN_HEIGHT__": f"{cls.shelf_scrollbar_min_height}px",
            "__SHELF_SCROLLBAR_BOTTOM_MARGIN__": f"{cls.shelf_scrollbar_bottom_margin}px",
            "__SHELF_SCROLLBAR_HANDLE_HIDDEN__": cls._rgba_qss(cls.color_scrollbar_handle_hidden_rgba),
            "__SHELF_SCROLLBAR_HANDLE__": cls._rgba_qss(cls.color_scrollbar_handle_rgba),
            "__SHELF_SCROLLBAR_HANDLE_HOVER__": cls._rgba_qss(cls.color_scrollbar_handle_hover_rgba),
        }

    @staticmethod
    def _rgba_qss(value: tuple[int, int, int, int]) -> str:
        return f"rgba({value[0]}, {value[1]}, {value[2]}, {value[3]})"
