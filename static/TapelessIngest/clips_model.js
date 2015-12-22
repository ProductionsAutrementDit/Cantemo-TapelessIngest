! function(ClipTable) {

    ClipTable.Clip = Backbone.Model.extend({
        defaults: {
            ui_selected: !1,
            id: "",
            name: "",
            provider_name: "",
            timecode: "",
            duration: "",
            duration_readable: "",
            progress: "",
            folder_path: "",
            status: "",
            state: "",
            error: "",
            status_readable: "",
            item_id: "",
            file_id: "",
            collection_id: "",
            user: 1,
            count_spanned: 0,
            metadatas: [],
            thumbnail_url: ""
        },
        ingest: function(opts){
            var self = this,
            url = self.url() + '/ingest',
            // note that these are just $.ajax() options
            options = {
                url: url,
                type: 'POST' // see my note below
            };
            // add any additional options, e.g. a "success" callback or data
            _.extend(options, opts);
            return (this.fetch || Backbone.fetch).call(this, options);
        },
        actualize: function(opts){
            var self = this,
            url = self.url() + '/actualize',
            // note that these are just $.ajax() options
            options = {
                url: url,
                type: 'POST' // see my note below
            };
            // add any additional options, e.g. a "success" callback or data
            _.extend(options, opts);
            return (this.fetch || Backbone.fetch).call(this, options);
        },
        toggle: function() {
            this.set({
                ui_selected: !this.get("ui_selected")
            })
        },
        clear: function() {
            var self = this;
            this.destroy({
                success: function() {
                    self.view.remove()
                },
                error: function(model, response) {
                    $.growl(response.responseText, "error")
                },
                wait: !0
            })
        },
        parse: function(response) {
          var clip = response;
          clip.id = response.umid;
          _.each(response.metadatas, function(metadata) {
            clip.metadatas[metadata.name] = metadata.value
          })
          clip.count_spanned = clip.spanned_clips.length
          return clip;
        }
    })

}(cntmo.prtl.ClipTable = cntmo.prtl.ClipTable || {}, jQuery);