<?php
add_action('wp_enqueue_scripts', function() {
    wp_enqueue_style('parent-style', get_template_directory_uri() . '/../twentytwentyfive/style.css');

    // Roboto / Roboto Slab match the fkcc.online design this theme is modeled on.
    wp_enqueue_style(
        'gpcc-google-fonts',
        'https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;600;700&family=Roboto+Slab:wght@400;600;700&display=swap',
        [],
        null
    );

    wp_enqueue_style(
        'gpcc-style',
        get_stylesheet_uri(),
        ['parent-style'],
        wp_get_theme()->get('Version')
    );
});

// Renders the single soonest upcoming Meeting-category event, for the
// front page's "Next Meeting" box - mirrors the "Latest From GPCC" card
// next to it. Events Manager's own [events_list] shortcode ignores its
// "format" attribute for the default "list" view, so this builds the card
// markup directly from EM_Event/EM_Location instead.
add_shortcode('gpcc_next_meeting', function () {
    if (!class_exists('EM_Events')) {
        return '';
    }

    $events = EM_Events::get([
        'category' => 'meeting',
        'scope' => 'future',
        'limit' => 1,
        'orderby' => 'event_start_date',
        'order' => 'ASC',
    ]);

    $event = null;
    foreach ($events as $next) {
        $event = $next;
        break;
    }

    if (!$event) {
        return '<div class="wp-block-group gpcc-next-meeting"><div class="gpcc-next-meeting-body">'
            . '<p>No upcoming meetings are currently scheduled - check the '
            . '<a href="' . esc_url(home_url('/meetings/')) . '">Meetings page</a> for updates.</p>'
            . '</div></div>';
    }

    $location = $event->get_location();
    $location_line = $location ? $location->output('#_LOCATIONNAME, #_LOCATIONADDRESS, #_LOCATIONTOWN') : '';
    $permalink = esc_url($event->get_permalink());

    // Built as a single concatenated string, not a template with blank
    // lines between PHP tags - wpautop() (part of the_content filters that
    // run over wp:shortcode block output) inserts stray <p>/</p> around
    // anything it sees separated by blank lines, which mangles nested
    // block-level HTML like this into invalid markup.
    $html = '<div class="wp-block-group gpcc-next-meeting"><div class="gpcc-next-meeting-body">';
    $html .= '<p class="gpcc-latest-post-date">' . esc_html($event->output('#_EVENTDATES')) . '</p>';
    $html .= '<h3><a href="' . $permalink . '">' . esc_html($event->event_name) . '</a></h3>';
    if ($location_line) {
        $html .= '<p>' . esc_html($location_line) . '</p>';
    }
    $html .= '<p><a class="gpcc-read-more" href="' . $permalink . '">View meeting details &rarr;</a></p>';
    $html .= '</div></div>';

    return $html;
});
