<?php
/**
 * Plugin Name: Polly PDF
 * Description: Powered by SeaMonster Studios. Extracts images from uploads and performs serverless structural PDF/UA alt-tagging.
 * Version: 0.4.0
 * Author: SeaMonster Studios
 * Author URI: https://www.seamonsterstudios.com
 * Text Domain: polly-pdf
 */

if ( ! defined( 'ABSPATH' ) ) exit;

define( 'POLLY_PDF_VERSION', '0.4.0' );
define( 'POLLY_PDF_PLUGIN_FILE', __FILE__ );

function polly_pdf_get_available_models() {
    $api_key = get_option( 'polly_pdf_api_key', '' );
    if ( empty( $api_key ) ) {
        return [];
    }

    $transient_key = 'polly_pdf_models_list';
    $cached = get_transient( $transient_key );
    if ( false !== $cached ) {
        return $cached;
    }

    $url = 'https://generativelanguage.googleapis.com/v1beta/models?key=' . esc_attr( $api_key );
    $response = wp_remote_get( $url, [ 'timeout' => 15 ] );

    if ( is_wp_error( $response ) ) {
        return [ 'gemini-2.0-flash' => 'Gemini 2.0 Flash (Fallback)' ];
    }

    $body = wp_remote_retrieve_body( $response );
    $data = json_decode( $body, true );

    if ( empty( $data['models'] ) || ! is_array( $data['models'] ) ) {
        return [ 'gemini-2.0-flash' => 'Gemini 2.0 Flash (Fallback)' ];
    }

    $models = [];
    foreach ( $data['models'] as $m ) {
        if ( empty( $m['name'] ) || empty( $m['supportedGenerationMethods'] ) ) {
            continue;
        }
        if ( ! in_array( 'generateContent', $m['supportedGenerationMethods'], true ) ) {
            continue;
        }
        $clean_name = str_replace( 'models/', '', $m['name'] );
        if ( 
            strpos( $clean_name, 'embedding' ) !== false || 
            strpos( $clean_name, 'text' ) !== false || 
            strpos( $clean_name, 'tuning' ) !== false
        ) {
            continue;
        }
        $display_name = ! empty( $m['displayName'] ) ? $m['displayName'] : $clean_name;
        $models[ $clean_name ] = $display_name;
    }

    if ( empty( $models ) ) {
        $models = [ 'gemini-2.0-flash' => 'Gemini 2.0 Flash (Fallback)' ];
    }

    set_transient( $transient_key, $models, DAY_IN_SECONDS );
    return $models;
}

// =============================================================================
// 2. Add Menus
// =============================================================================

add_action( 'admin_menu', function () {
    add_menu_page(
        'Polly PDF Remediation',
        'Polly PDF',
        'manage_options',
        'polly-pdf-workspace',
        'polly_pdf_workspace_page',
        'dashicons-pdf',
        30
    );

    add_submenu_page(
        'polly-pdf-workspace',
        'Polly PDF Settings',
        'Settings',
        'manage_options',
        'polly-pdf-settings',
        'polly_pdf_settings_page'
    );
} );

add_filter( 'plugin_action_links_' . plugin_basename( POLLY_PDF_PLUGIN_FILE ), function ( $links ) {
    $settings_link = '<a href="' . esc_url( admin_url( 'admin.php?page=polly-pdf-settings' ) ) . '">'
        . __( 'Settings' ) . '</a>';
    array_unshift( $links, $settings_link );
    return $links;
} );

add_action( 'admin_init', function () {
    register_setting( 'polly_pdf_group', 'polly_pdf_api_key' );
    register_setting( 'polly_pdf_group', 'polly_pdf_model' );
    register_setting( 'polly_pdf_group', 'polly_pdf_server_url' );
    register_setting( 'polly_pdf_group', 'polly_pdf_choice_count', [
        'sanitize_callback' => function( $val ) {
            $v = intval( $val );
            return max( 1, min( 6, $v ) );
        }
    ] );

    add_settings_section( 'polly_pdf_main_section', "Configuration", null, 'polly-pdf-settings' );

    add_settings_field( 'api_key', 'Gemini API Key', function () {
        $val = get_option( 'polly_pdf_api_key', '' );
        ?>
        <input type="password" name="polly_pdf_api_key" value="<?php echo esc_attr( $val ); ?>" class="regular-text" autocomplete="off">
        <p class="description">Get your API key at <a href="https://aistudio.google.com/app/apikey" target="_blank">Google AI Studio</a>.</p>
        <?php
    }, 'polly-pdf-settings', 'polly_pdf_main_section' );

    add_settings_field( 'choice_count', 'Alt Text Choices', function () {
        $val = intval( get_option( 'polly_pdf_choice_count', 3 ) );
        ?>
        <input type="number" name="polly_pdf_choice_count" value="<?php echo esc_attr( $val ); ?>" min="1" max="6" style="width: 60px;">
        <p class="description">How many AI-generated alt text options to show per image (default: 3).</p>
        <?php
    }, 'polly-pdf-settings', 'polly_pdf_main_section' );

    add_settings_field( 'model', 'AI Model', function () {
        $models = polly_pdf_get_available_models();
        if ( empty( $models ) ) {
            $models = [
                'gemini-2.0-flash' => 'Gemini 2.0 Flash',
                'gemini-1.5-flash' => 'Gemini 1.5 Flash',
            ];
        }

        $recommended_model = '';
        foreach ( array_keys( $models ) as $model_key ) {
            if ( strpos( $model_key, '-flash' ) !== false ) {
                if ( empty( $recommended_model ) || version_compare( $model_key, $recommended_model, '>' ) ) {
                    $recommended_model = $model_key;
                }
            }
        }
        
        if ( empty( $recommended_model ) ) {
            $model_keys = array_keys( $models );
            $recommended_model = ! empty( $model_keys ) ? $model_keys[0] : '';
        }

        $current_model = get_option( 'polly_pdf_model', $recommended_model );
        ?>
        <select name="polly_pdf_model" id="polly-pdf-model" style="min-width: 250px;">
            <?php foreach ( $models as $value => $label ) : 
                $display_label = ( $recommended_model === $value ) ? $label . ' (Recommended)' : $label;
                ?>
                <option value="<?php echo esc_attr( $value ); ?>" <?php selected( $current_model, $value ); ?>>
                    <?php echo esc_html( $display_label ); ?>
                </option>
            <?php endforeach; ?>
        </select>
        <p class="description">
            Select the active Gemini model for PDF remediation. This list stays updated dynamically via Google's live endpoints.
        </p>
        <?php
    }, 'polly-pdf-settings', 'polly_pdf_main_section' );

    add_settings_field( 'server_url', 'Remediation Server URL', function () {
        $val = get_option( 'polly_pdf_server_url', 'http://localhost:5001' );
        ?>
        <input type="url" name="polly_pdf_server_url" value="<?php echo esc_url( $val ); ?>" class="regular-text" placeholder="http://localhost:5001">
        <p class="description">
            Your Python backend endpoint. Keep as <code>http://localhost:5001</code> for local testing, or change to SeaMonster's AWS Gateway URL once deployed!
        </p>
        <?php
    }, 'polly-pdf-settings', 'polly_pdf_main_section' );
} );

function polly_pdf_settings_page() {
    ?>
    <div class="wrap">
        <h1>⚙️ Polly PDF Settings</h1>
        <form method="post" action="options.php">
            <?php
            settings_fields( 'polly_pdf_group' );
            do_settings_sections( 'polly-pdf-settings' );
            submit_button();
            ?>
        </form>
    </div>
    <?php
}

add_action('wp_ajax_polly_pdf_proxy', function() {
    if (!check_ajax_referer('polly_pdf_nonce', 'nonce', false)) {
        wp_send_json_error(['message' => 'Security check failed.'], 403);
    }
    if (!current_user_can('upload_files')) {
        wp_send_json_error(['message' => 'Permission denied.'], 403);
    }

    $api_key = get_option('polly_pdf_api_key', '');
    if (!$api_key) {
        wp_send_json_error(['message' => 'No API key configured.'], 400);
    }

    $model = sanitize_text_field(wp_unslash($_POST['model'] ?? 'gemini-2.0-flash'));
    $body  = wp_unslash($_POST['payload'] ?? '');

    $response = wp_remote_post(
        "https://generativelanguage.googleapis.com/v1beta/models/{$model}:generateContent?key={$api_key}",
        [
            'headers' => ['Content-Type' => 'application/json'],
            'body'    => $body,
            'timeout' => 30,
        ]
    );

    if (is_wp_error($response)) {
        wp_send_json_error(['message' => $response->get_error_message()], 500);
    }

    $code = wp_remote_retrieve_response_code($response);
    $data = json_decode(wp_remote_retrieve_body($response), true);

    if ($code !== 200) {
        wp_send_json_error(['message' => $data['error']['message'] ?? 'Gemini error.'], $code);
    }

    wp_send_json_success($data);
});

function polly_pdf_workspace_page() {
    ?>
    <div class="wrap" style="margin-top: 20px;">
        <h1 style="margin-bottom: 20px;">🦜 Polly PDF Workspace</h1>
        
        <div id="polly-workspace-container" style="display: flex; height: calc(100vh - 160px); background: #f6f7f7; border: 1px solid #ccd0d4; box-shadow: 0 1px 1px rgba(0,0,0,.04); overflow: hidden;">
            
            <div id="gallery" style="width: 420px; background: white; border-right: 1px solid #ccd0d4; display: flex; flex-direction: column; box-shadow: 2px 0 5px rgba(0,0,0,0.02); z-index: 10;">
                <div class="gallery-header" style="padding: 20px; border-bottom: 1px solid #ccd0d4; background: #0073aa; color: white;">
                    <h2 style="margin: 0; font-size: 1.2rem; color: white; line-height: 1.2;">Polly Gallery</h2>
                    <p id="image-count" style="margin: 5px 0 0; opacity: 0.8; font-size: 0.85rem;">No PDF loaded</p>
                </div>
                
                <div id="image-list" style="flex-grow: 1; overflow-y: auto; padding: 15px;">
                    <div id="placeholder-msg" style="text-align: center; color: #888; margin-top: 50px; padding: 20px;">
                        <p>Drop a PDF document into the workspace to scan for assets.</p>
                    </div>
                </div>
            </div>

            <div id="workspace" style="flex-grow: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; position: relative; background: #f6f7f7; padding: 40px; box-sizing: border-box;">
                <div id="drop-zone" style="width: 100%; max-width: 600px; height: 350px; border: 3px dashed #0073aa; border-radius: 20px; display: flex; flex-direction: column; align-items: center; justify-content: center; color: #0073aa; transition: all 0.3s; text-align: center; padding: 20px; background: white; box-sizing: border-box;">
                    
                    <div id="idle-view">
                        <svg width="64" height="64" viewBox="0 0 24 24" fill="currentColor" style="margin-bottom: 20px; opacity: 0.3; display: inline-block;">
                            <path d="M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2M18,20H6V4H13V9H18V20Z"/>
                        </svg>
                        <h3 style="margin: 0 0 10px; font-size: 1.4rem;">System Ready</h3>
                        <p style="margin: 0; color: #666;">Drag and drop a PDF file here.</p>
                    </div>
                    
                    <div id="loading-view" class="hidden">
                        <div class="spinner" style="margin: 0 auto 20px; width: 40px; height: 40px; border: 3px solid #f3f3f3; border-top: 3px solid #0073aa; border-radius: 50%; animation: spin 1s linear infinite;"></div>
                        <h3 id="loading-title">Analyzing PDF...</h3>
                        <p id="loading-sub" style="margin: 0; color: #666;">Processing page streams...</p>
                    </div>

                    <div id="ready-view" class="hidden" style="width: 100%;">
                        <div id="compliance-info" style="margin-bottom: 20px;">
                            <span id="doc-type-badge" class="badge" style="display: inline-block; padding: 6px 12px; border-radius: 4px; font-weight: bold; text-transform: uppercase; font-size: 0.75rem;">Checking Structure...</span>
                        </div>
                        <h3 id="ready-title">PDF Processed</h3>
                        <p id="ready-sub" style="margin: 0 0 20px; color: #666;">0 images remediated.</p>
                        <button class="primary download-btn" id="download-btn" disabled style="background: #46b450; color: white; border: none; padding: 15px 30px; border-radius: 4px; cursor: pointer; font-weight: bold; display: flex; align-items: center; justify-content: center; gap: 10px; margin: 0 auto; font-size: 1rem;">
                            <svg width="20" height="20" viewBox="0 0 24 24" fill="white"><path d="M5,20H19V18H5M19,9H15V3H9V9H5L12,16L19,9Z"/></svg>
                            Download Remediated PDF
                        </button>
                    </div>
                </div>

                <div id="status-bar" style="position: absolute; bottom: 0; left: 0; right: 0; padding: 10px 20px; background: white; border-top: 1px solid #ccd0d4; font-size: 0.85rem; display: flex; justify-content: space-between;">
                    <span id="status-left">Polly PDF v0.4.0</span>
                    <span id="status-right" style="font-weight: 600;">Idle</span>
                </div>
            </div>
        </div>
    </div>
    <?php
}

add_action( 'admin_enqueue_scripts', function ( $hook ) {
    if ( 'toplevel_page_polly-pdf-workspace' !== $hook ) return;

    wp_enqueue_style(
        'polly-pdf-styles',
        plugin_dir_url( POLLY_PDF_PLUGIN_FILE ) . 'polly-pdf.css',
        [],
        POLLY_PDF_VERSION
    );

    wp_enqueue_script(
        'pdf-lib-script',
        'https://cdnjs.cloudflare.com/ajax/libs/pdf-lib/1.17.1/pdf-lib.min.js',
        [],
        null,
        false
    );

    wp_enqueue_script(
        'pdf-js-script',
        'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js',
        [],
        null,
        false
    );

    wp_enqueue_script(
        'polly-pdf-logic',
        plugin_dir_url( POLLY_PDF_PLUGIN_FILE ) . 'polly-pdf.js',
        [],
        POLLY_PDF_VERSION,
        true
    );

    wp_localize_script( 'polly-pdf-logic', 'pollyPdfConfig', [
        'model'       => get_option( 'polly_pdf_model', 'gemini-2.0-flash' ),
        'serverUrl'   => get_option( 'polly_pdf_server_url', 'http://localhost:5001' ),
        'choiceCount' => intval( get_option( 'polly_pdf_choice_count', 3 ) ),
        'ajaxUrl'     => admin_url( 'admin-ajax.php' ),
        'nonce'       => wp_create_nonce( 'polly_pdf_nonce' )
    ] );
} );