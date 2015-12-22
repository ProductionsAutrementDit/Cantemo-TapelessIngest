!function(Collection, $, undefined) {
    Collection.addTargetCollection = Backbone.View.extend({
        tagName: "div",
        events: {},
        initialize: function(options) {
            this.collectionaddbuttontext = options.collectionaddbuttontext || gettext("Define Target Collection"), this.collectioncancelbuttontext = options.collectioncancelbuttontext || gettext("Cancel"), this.title = options.title || gettext("Define Target Collection"), this.formURL = options.formURL || "/tapelessingest/addtargetcollectionform", this.searchCollectionsURL = options.searchCollectionsURL || "/vs/search_collections", this.dialogButtons = {}, this.dialogOptions = options.dialogoptions || {}, this.getForm(), this.open(), this.selected_objects = options.selected_objects || []
        },
        getForm: function() {
            var self = this;
            $.get(this.formURL, function(data) {
                self.$el.html(data), self.$smartSelectBox = self.$el.find("#targetcollectionselect"), self.smartSelectBox = $(self.$smartSelectBox).AjaxCollectionSelect({
                    valsep: "*valsep*",
                    keysep: "*keysep*"
                })
            })
        },
        open: function() {
            var standardOptions, self = this;
            self.dialogButtons = [{
                text: self.collectionaddbuttontext,
                click: function() {
                    self.add()
                },
                "class": "add-to-collection-button ui-dialog-button-confirm"
            }, {
                text: self.collectioncancelbuttontext,
                click: function() {
                    self.close()
                },
                "class": "cancel-add-to-collection-button ui-dialog-button-cancel"
            }
            ], standardOptions = {
                modal: !0,
                resizable: !1,
                dialogClass: "addTargetCollection",
                title: self.title,
                minWidth: "450",
                minHeight: "200",
                show: {
                    effect: "fade",
                    duration: 500
                },
                hide: {
                    effect: "fade",
                    duration: 500
                },
                buttons: self.dialogButtons
            };
            for (var attrname in self.dialogOptions)
                standardOptions[attrname] = self.dialogOptions[attrname];
            self.$el.dialog(standardOptions)
        },
        add: function() {
            var self = this, form = this.$el.find("form#collection_add_target_form");
            self.smartSelectBox ? (formdata = {
                selected_objects: self.selected_objects,
                collection: self.smartSelectBox.val(),
                collectionprofilegroup: form.find("#collectionprofilechooser").val(),
            }, $.ajax({
                type: "POST",
                url: form.attr("action"),
                data: formdata,
                traditional: !0,
                success: function(responseText) {
                    $.growl(responseText.success, "success"), self.close()
                },
                error: function(responseText) {
                    $.growl(JSON.parse(responseText.responseText).error, "error")
                }
            })) : $.growl("There was an error sending to the backend", "error")
        },
        close: function() {
            this.$el.dialog("close"), this.undelegateEvents(), $(this.el).removeData().unbind(), this.remove(), Backbone.View.prototype.remove.call(this)
        }
    })
}(cntmo.prtl.Collection = cntmo.prtl.Collection || {}, jQuery);

