! function(FolderTable) {

    FolderTable.Folder = Backbone.Model.extend({
        defaults: {
          storage: "",
          basename: "",
          path: ""
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
          var folder = response;
          folder.id = response.id;
          return folder;
        }
    })

}(cntmo.prtl.FolderTable = cntmo.prtl.FolderTable || {}, jQuery);
